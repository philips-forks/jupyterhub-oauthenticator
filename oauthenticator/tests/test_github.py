import functools
import json
import logging
import re
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from pytest import fixture, mark
from tornado.httpclient import HTTPResponse
from tornado.httputil import HTTPHeaders
from traitlets.config import Config

from ..github import GitHubOAuthenticator
from .mocks import setup_oauth_mock


def user_model(username):
    """Return a user model"""
    return {
        'email': 'dinosaurs@space',
        'id': 5,
        'login': username,
        'name': 'Hoban Washburn',
    }


@fixture
def github_client(client):
    setup_oauth_mock(
        client,
        host=['github.com', 'api.github.com'],
        access_token_path='/login/oauth/access_token',
        user_path='/user',
        token_type='token',
    )
    return client


async def test_github(github_client):
    authenticator = GitHubOAuthenticator()
    handler = github_client.handler_for_user(user_model('wash'))
    auth_model = await authenticator.get_authenticated_user(handler, None)
    assert auth_model['name'] == 'wash'
    auth_state = auth_model['auth_state']
    assert 'access_token' in auth_state
    assert 'github_user' in auth_state
    assert auth_state["github_user"] == {
        'email': 'dinosaurs@space',
        'id': 5,
        'login': auth_model['name'],
        'name': 'Hoban Washburn',
    }


def make_link_header(urlinfo, page):
    return {
        "Link": f'<{urlinfo.scheme}://{urlinfo.netloc}{urlinfo.path}?page={page}>;rel="next"'
    }


async def test_allowed_org_membership(github_client):
    client = github_client
    authenticator = GitHubOAuthenticator()

    ## Mock Github API

    orgs = {
        'red': ['grif', 'simmons', 'donut', 'sarge', 'lopez'],
        'blue': ['tucker', 'caboose', 'burns', 'sheila', 'texas'],
    }

    org_teams = {'blue': {'alpha': ['tucker', 'caboose', 'burns']}}

    member_regex = re.compile(r'/orgs/(.*)/members')

    def org_members(paginate, request):
        urlinfo = urlparse(request.url)
        org = member_regex.match(urlinfo.path).group(1)

        if org not in orgs:
            return HTTPResponse(request, 404)

        if not paginate:
            return [user_model(m) for m in orgs[org]]
        else:
            page = parse_qs(urlinfo.query).get('page', ['1'])
            page = int(page[0])
            return org_members_paginated(
                org, page, urlinfo, functools.partial(HTTPResponse, request)
            )

    def org_members_paginated(org, page, urlinfo, response):
        if page < len(orgs[org]):
            headers = make_link_header(urlinfo, page + 1)
        elif page == len(orgs[org]):
            headers = {}
        else:
            return response(400)

        headers.update({'Content-Type': 'application/json'})

        ret = [user_model(orgs[org][page - 1])]

        return response(
            200,
            headers=HTTPHeaders(headers),
            buffer=BytesIO(json.dumps(ret).encode('utf-8')),
        )

    org_membership_regex = re.compile(r'/orgs/(.*)/members/(.*)')

    def org_membership(request):
        urlinfo = urlparse(request.url)
        urlmatch = org_membership_regex.match(urlinfo.path)
        org = urlmatch.group(1)
        username = urlmatch.group(2)
        print(f"Request org = {org}, username = {username}")
        if org not in orgs:
            print(f"Org not found: org = {org}")
            return HTTPResponse(request, 404)
        if username not in orgs[org]:
            print(f"Member not found: org = {org}, username = {username}")
            return HTTPResponse(request, 404)
        return HTTPResponse(request, 204)

    team_membership_regex = re.compile(r'/orgs/(.*)/teams/(.*)/members/(.*)')

    def team_membership(request):
        urlinfo = urlparse(request.url)
        urlmatch = team_membership_regex.match(urlinfo.path)
        org = urlmatch.group(1)
        team = urlmatch.group(2)
        username = urlmatch.group(3)
        print(f"Request org = {org}, team = {team} username = {username}")
        if org not in orgs:
            print(f"Org not found: org = {org}")
            return HTTPResponse(request, 404)
        if team not in org_teams[org]:
            print(f"Team not found in org: team = {team}, org = {org}")
            return HTTPResponse(request, 404)
        if username not in org_teams[org][team]:
            print(
                f"Member not found: org = {org}, team = {team}, username = {username}"
            )
            return HTTPResponse(request, 404)
        return HTTPResponse(request, 204)

    ## Perform tests

    for paginate in (False, True):
        client_hosts = client.hosts['api.github.com']
        client_hosts.append((team_membership_regex, team_membership))
        client_hosts.append((org_membership_regex, org_membership))
        client_hosts.append((member_regex, functools.partial(org_members, paginate)))

        authenticator.allowed_organizations = ['blue']

        handler = client.handler_for_user(user_model('caboose'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model['name'] == 'caboose'

        handler = client.handler_for_user(user_model('donut'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model is None

        # reverse it, just to be safe
        authenticator.allowed_organizations = ['red']

        handler = client.handler_for_user(user_model('caboose'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model is None

        handler = client.handler_for_user(user_model('donut'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model['name'] == 'donut'

        # test team membership
        authenticator.allowed_organizations = ['blue:alpha', 'red']

        handler = client.handler_for_user(user_model('tucker'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model['name'] == 'tucker'

        handler = client.handler_for_user(user_model('grif'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model['name'] == 'grif'

        handler = client.handler_for_user(user_model('texas'))
        auth_model = await authenticator.get_authenticated_user(handler, None)
        assert auth_model is None

        client_hosts.pop()
        client_hosts.pop()


@mark.parametrize(
    "org, username, expected",
    [
        ("blue", "texas", "https://api.github.com/orgs/blue/members/texas"),
        (
            "blue:alpha",
            "tucker",
            "https://api.github.com/orgs/blue/teams/alpha/members/tucker",
        ),
        ("red", "grif", "https://api.github.com/orgs/red/members/grif"),
    ],
)
async def test_build_check_membership_url(org, username, expected):
    output = GitHubOAuthenticator()._build_check_membership_url(org, username)
    assert output == expected


async def test_deprecated_config(caplog):
    cfg = Config()
    cfg.GitHubOAuthenticator.github_organization_whitelist = ["jupy"]
    cfg.Authenticator.whitelist = {"user1"}

    log = logging.getLogger("testlog")
    authenticator = GitHubOAuthenticator(config=cfg, log=log)
    assert (
        log.name,
        logging.WARNING,
        'GitHubOAuthenticator.github_organization_whitelist is deprecated in GitHubOAuthenticator 0.12.0, use '
        'GitHubOAuthenticator.allowed_organizations instead',
    ) in caplog.record_tuples

    assert authenticator.allowed_organizations == {"jupy"}
    assert authenticator.allowed_users == {"user1"}
