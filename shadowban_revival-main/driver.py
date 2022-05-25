import aiohttp
import argparse
import asyncio
import daemon
import json
import os
import re
import traceback
import urllib.parse
import urllib
import sys
import time
import random

from aiohttp import web
from bs4 import BeautifulSoup


"""
archive.org link for shadowban's frontend: 
    https://web.archive.org/web/20210215061542/https://shadowban.eu/

current status:
    Twitter public api key is still the same.
    guest token fetching process is exactly the same.
    typeahead fetch has changed slightly
    Getting full profile & w/replies has changed to graphQL
    search has changed slightly
"""
# This is a public value from the Twitter source code.
TWITTER_AUTH_KEY = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'


routes = web.RouteTableDef()


class UnexpectedAPIError(Exception):
    pass


def get_nested(obj, path, default=None):
    for p in path:
        if obj is None or not p in obj:
            return default
        obj = obj[p]
    return obj


"""
this function just checks for a set of errors 
that appear in the "errors" field of the result json
suggesting that twitter responded with errors
"""
def is_error(result, code=None):
    return isinstance(result.get("errors", None), list) and (len([x for x in result["errors"] if x.get("code", None) == code]) > 0 or code is None and len(result["errors"] > 0))

# this one seems to be the negation of that function?
# based on the not in
# this is also ONLY used in one place to make sure all the errors
# are expected
def is_another_error(result, codes):
    return isinstance(result.get("errors", None), list) and len([x for x in result["errors"] if x.get("code", None) not in codes]) > 0


log_file = None
debug_file = None
guest_sessions = []


class TwitterSession:
    twitter_auth_key = None

    def __init__(self):
        self._guest_token = None
        self._csrf_token = None

        # aiohttp ClientSession
        self._session = None

        # rate limit monitoring
        self.limit = -1
        self.remaining = 180
        self.reset = -1
        self.overshot = -1
        self.locked = False
        self.next_refresh = None

        # session user's @username
        # this stays `None` for guest sessions
        self.username = None

        self._headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/75.0.3770.100 Safari/537.36"
        }
        # sets self._headers
        self.reset_headers()

    def set_csrf_header(self):
        cookies = self._session.cookie_jar.filter_cookies(
            'https://twitter.com/')
        for key, cookie in cookies.items():
            if cookie.key == 'ct0':
                self._headers['X-Csrf-Token'] = cookie.value

    async def get_guest_token(self):
        self._headers['Authorization'] = 'Bearer ' + self.twitter_auth_key
        async with self._session.post("https://api.twitter.com/1.1/guest/activate.json", headers=self._headers) as r:
            response = await r.json()
        guest_token = response.get("guest_token", None)
        if guest_token is None:
            debug("Failed to fetch guest token")
            debug(str(response))
            debug(str(self._headers))
        async with self._session.post("https://api.twitter.com/1.1/guest/activate.json", headers=self._headers) as r:
            response = await r.json()
        return guest_token

    def reset_headers(self):
        self._headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/75.0.3770.100 Safari/537.36"
        }

    async def renew_session(self):
        await self.try_close()
        # await this one ????
        self._session = aiohttp.ClientSession()
        self.reset_headers()

    async def refresh_old_token(self):
        if self.username is not None or self.next_refresh is None or time.time() < self.next_refresh:
            return
        debug("Refreshing token: " + str(self._guest_token))
        await self.login_guest()
        debug("New token: " + str(self._guest_token))

    async def try_close(self):
        if self._session is not None:
            try:
                await self._session.close()
            except:
                pass

    async def login_guest(self):
        await self.renew_session()
        self.set_csrf_header()
        old_token = self._guest_token
        new_token = await self.get_guest_token()
        self._guest_token = new_token if new_token is not None else old_token
        if new_token is not None:
            self.next_refresh = time.time() + 3600
        # header config for guests
        self._headers['X-Guest-Token'] = self._guest_token
        self._headers['Authorization'] = 'Bearer ' + self.twitter_auth_key

    async def post(self, url, prms):
        self.set_csrf_header()
        await self.refresh_old_token()
        try:
            assert self._session is not None
            assert self._headers is not None
            async with self._session.post(url, headers=self._headers, params=prms) as r:
                print("rr", r.status)
                result = await r.json()
        except Exception as e:
            debug("EXCEPTION: " + str(type(e)))
            debug("EXCEPTION text: " + str(e))
            raise e

        return result

    async def get(self, url, retries=0):
        self.set_csrf_header()
        await self.refresh_old_token()
        try:
            assert self._session is not None
            assert self._headers is not None
            async with self._session.get(url, headers=self._headers) as r:
                print("rr", r.status)
                result = await r.json()
        except Exception as e:
            debug("EXCEPTION: " + str(type(e)))
            debug("EXCEPTION text: " + str(e))
            raise e
        self.monitor_rate_limit(r.headers)
        if self.username is None and self.remaining < 10 or is_error(result, 88) or is_error(result, 239):
            # get a fresh session depending on said factors?
            await self.login_guest()
        if retries > 0 and is_error(result, 353):
            # recursive retries
            return await self.get(url, retries - 1)
        if is_error(result, 326):
            # what does a locked session imply? we can't use it again? why not discard it at that point? # TODO
            self.locked = True
        return result

    async def search_raw(self, query, live=True):

        user_arg = urllib.parse.quote(query)
        query = f"https://twitter.com/i/api/2/search/adaptive.json?include_profile_interstitial_type=1&include_blocking=1&include_blocked_by=1&include_followed_by=1&include_want_retweets=1&include_mute_edge=1&include_can_dm=1&include_can_media_tag=1&include_ext_has_nft_avatar=1&skip_status=1&cards_platform=Web-12&include_cards=1&include_ext_alt_text=true&include_quote_count=true&include_reply_count=1&tweet_mode=extended&include_entities=true&include_user_entities=true&include_ext_media_color=true&include_ext_media_availability=true&include_ext_sensitive_media_warning=true&include_ext_trusted_friends_metadata=true&send_error_codes=true&simple_quoted_tweet=true&q={user_arg}&vertical=default&count=20&query_source=typd&pc=1&spelling_corrections=1&ext=mediaStats%2ChighlightedLabel%2ChasNftAvatar%2CvoiceInfo%2Cenrichments%2CsuperFollowMetadata"

        return await self.get(query)

    async def typeahead_raw(self, query):
        arg_query = f"https://twitter.com/i/api/1.1/search/typeahead.json?q={urllib.parse.quote(query)}&src=search_box&result_type=events%2Cusers%2Ctopics"
        return await self.get(arg_query)

    async def profile_raw(self, username):
        return await self.get("https://api.twitter.com/1.1/users/show.json?screen_name=" + urllib.parse.quote(username))

    async def get_profile_tweets_raw(self, user_id):
        variables = {
            "userId": user_id,
            "count": 40,
            "withTweetQuoteCount": True,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": False,
            "withSuperFollowsUserFields": False,
            "withUserResults": True,
            "withBirdwatchPivots": False,
            "withReactionsMetadata": False,
            "withReactionsPerspective": False,
            "withSuperFollowsTweetFields": False,
            "withVoice": True
        }

        params = {
            'variables': json.dumps(variables, sort_keys=True, indent=4, separators=(',', ':'))
        }

        return await self.post("https://twitter.com/i/api/graphql/9R7ABsb6gQzKjl5lctcnxA/UserTweets", params)

    async def tweet_raw(self, tweet_id, count=20, cursor=None, retry_csrf=True):
        variables = {
            "focalTweetId": tweet_id, "referrer": "profile", "with_rux_injections": True, "includePromotedContent": True, "withCommunity": True, "withQuickPromoteEligibilityTweetFields": True, "withBirdwatchNotes": False, "withSuperFollowsUserFields": True, "withDownvotePerspective": False, "withReactionsMetadata": False, "withReactionsPerspective": False, "withSuperFollowsTweetFields": True, "withVoice": True, "withV2Timeline": False, "__fs_dont_mention_me_view_api_enabled": True, "__fs_interactive_text_enabled": True, "__fs_responsive_web_uc_gql_enabled": False
        }

        params = {
            'variables': json.dumps(variables, sort_keys=True, indent=4, separators=(',', ':'))
        }

        return await self.post("https://twitter.com/i/api/graphql/LJ_TjoWGgNTXCl7gfx4Njw/TweetDetail", params)

    def monitor_rate_limit(self, headers):
        # store last remaining count for reset detection
        last_remaining = self.remaining
        limit = headers.get('x-rate-limit-limit', None)
        remaining = headers.get('x-rate-limit-remaining', None)
        reset = headers.get('x-rate-limit-reset', None)
        if limit is not None:
            self.limit = int(limit)
        if remaining is not None:
            self.remaining = int(remaining)
        if reset is not None:
            self.reset = int(reset)

        # rate limit reset
        if last_remaining < self.remaining and self.overshot > 0 and self.username is not None:
            log('[rate-limit] Reset detected for ' +
                self.username + '. Saving overshoot count...')
            self.overshot = 0

        # count the requests that failed because of rate limiting
        if self.remaining == 0:
            log('[rate-limit] Limit hit by ' + str(self.username) + '.')
            self.overshot += 1

    @classmethod
    def flatten_timeline(cls, timeline_items):
        result = []
        for item in timeline_items:
            if get_nested(item, ["content", "item", "content", "tweet", "id"]) is not None:
                result.append(item["content"]["item"]
                              ["content"]["tweet"]["id"])
            elif get_nested(item, ["content", "timelineModule", "items"]) is not None:
                timeline_items = item["content"]["timelineModule"]["items"]
                titems = [get_nested(x, ["item", "content", "tweet", "id"])
                          for x in timeline_items]
                result += [x for x in titems if x is not None]
        return result

    @classmethod
    def get_ordered_tweet_ids(cls, obj, filtered=True):
        try:
            entries = [x for x in obj["timeline"]["instructions"]
                       if "addEntries" in x][0]["addEntries"]["entries"]
        except (IndexError, KeyError):
            return []
        entries.sort(key=lambda x: -int(x["sortIndex"]))
        flat = cls.flatten_timeline(entries)
        return [x for x in flat if not filtered or x in obj["globalObjects"]["tweets"]]

    async def get_user_tweet_graph(self, user_id):

        result = {
            "datasets": [{
                "label": 'interactions follower data',
                "data": [],
            }],
            "labels": []
        }
        # get some user tweets
        tweets = (await self.get_profile_tweets_raw(user_id))
        tweets = tweets["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][1]["entries"]

        tweet_data_res = []
        for tweet in tweets[1:30]:
            # tweet["sortIndex"] is the id of a tweet
            tweet_data_res.append(self.tweet_raw(tweet["sortIndex"]))
        # go through tweets and get data from them
        tweet_data_res = await asyncio.gather(*tweet_data_res)
        for tweet, data in zip(tweets[1:30], tweet_data_res):
            if "errors" in data:
                continue
            data = data["data"]["threaded_conversation_with_injections"]["instructions"][0]["entries"]

            for entry in data:
                if "conversationthread" not in entry["entryId"]:
                    continue

                for user_details in entry["content"]["items"]:
                    try:
                        interacted_user_data = user_details["item"]["itemContent"][
                            "tweet_results"]["result"]["core"]["user_results"]["result"]

                        if user_id == int(interacted_user_data["rest_id"]):
                            continue

                        interacted_user_data = interacted_user_data["legacy"]
                        result["datasets"][0]["data"].append(interacted_user_data["followers_count"])
                        result["labels"].append(interacted_user_data["screen_name"])
                    except:
                        pass

        return result

    async def test(self, username):
        result = {"timestamp": time.time()}
        profile = {}
        profile_raw = await self.profile_raw(username)
        debug('Testing ' + str(username))
        if is_another_error(profile_raw, [50, 63]):  # unexpected errors
            debug("Other error:" + str(username))
            raise UnexpectedAPIError

        # get user id
        try:
            user_id = str(profile_raw["id"])
        except KeyError:
            user_id = None

        # get screen name
        try:
            profile["screen_name"] = profile_raw["screen_name"]
        except KeyError:
            profile["screen_name"] = username

        # get profile restrictions ?
        # I assume this is for 18+ accounts
        # or it can be one of those accounts kept up for public interest
        try:
            profile["restriction"] = profile_raw["profile_interstitial_type"]
        except KeyError:
            pass

        # remove empty restrictions
        if profile.get("restriction", None) == "":
            del profile["restriction"]

        # protected account means private account in twitter
        try:
            profile["protected"] = profile_raw["protected"]
        except KeyError:
            pass

        # checks for suspension and existence
        profile["exists"] = not is_error(profile_raw, 50)
        suspended = is_error(profile_raw, 63)
        if suspended:
            profile["suspended"] = suspended

        # see if profile has any tweets
        try:
            profile["has_tweets"] = int(profile_raw["statuses_count"]) > 0
        except KeyError:
            profile["has_tweets"] = False

        result["profile"] = profile
        # Early termination depending on state of profile?
        # checks for existence, suspension, protection, or no tweet counts of a given profile
        if not profile["exists"] or profile.get("suspended", False) or profile.get("protected", False) or not profile.get('has_tweets'):
            return result

        result["tests"] = {}

        search_raw = await self.search_raw("from:@" + username)

        # checks for search ban
        result["tests"]["search"] = False
        try:
            tweets = search_raw["globalObjects"]["tweets"]
            for tweet_id, tweet in sorted(tweets.items(), key=lambda t: t[1]["id"], reverse=True):
                result["tests"]["search"] = str(tweet_id)
                break

        except (KeyError, IndexError):
            pass

        # This is for search suggestion ban
        # Link to their frontend for that: https://github.com/shadowban-eu/shadowban-eu-frontend/blob/edfd4a6034b417e9f831d9a4a6762555ed6251f5/src/js/main.js
        # typeahead means autocomplete in this context
        typeahead_raw = await self.typeahead_raw("@" + username)
        result["tests"]["typeahead"] = False
        try:
            result["tests"]["typeahead"] = len(
                [1 for user in typeahead_raw["users"] if user["screen_name"].lower() == username.lower()]) > 0
        except KeyError:
            pass

        try:
            result["graph"] = await self.get_user_tweet_graph(user_id)
        except e:
            result["graph"] = {}
            print(e)
        # ghost ban check
        result["tests"]["ghost"] = {"ban": False}

        # barrier check
        result["tests"]["more_replies"] = {"error": "EISGHOSTED"}

        return result

    async def close(self):
        await self._session.close()


def debug(message):
    if message.endswith('\n') is False:
        message = message + '\n'

    if debug_file is not None:
        debug_file.write(message)
        debug_file.flush()
    else:
        print(message)


def log(message):
    # ensure newline
    if message.endswith('\n') is False:
        message = message + '\n'

    if log_file is not None:
        log_file.write(message)
        log_file.flush()
    else:
        print(message)


@routes.get('/{screen_name}')
async def api(request):
    screen_name = request.match_info['screen_name']
    session = random.choice(guest_sessions)  # pick a random guest session
    result = await session.test(screen_name)
    log(json.dumps(result) + '\n')
    args.cors_allow = '*'
    if (args.cors_allow is not None):
        return web.json_response(result, headers={"Access-Control-Allow-Origin": args.cors_allow})
    else:
        return web.json_response(result)


async def login_guests():
    guest_session_pool_size = 10
    for i in range(0, guest_session_pool_size):
        session = TwitterSession()
        guest_sessions.append(session)
    # login to every guest session!
    await asyncio.gather(*[s.login_guest() for s in guest_sessions])
    log("Guest sessions created")


parser = argparse.ArgumentParser(description='Twitter Shadowban Tester')
parser.add_argument('--account-file', type=str, default='.htaccounts',
                    help='json file with reference account credentials')
parser.add_argument('--cookie-dir', type=str, default=None,
                    help='directory for session account storage')
parser.add_argument('--log', type=str, default=None,
                    help='log file where test results are written to')
parser.add_argument('--daemon', action='store_true', help='run in background')
parser.add_argument('--debug', type=str, default=None, help='debug log file')
parser.add_argument('--port', type=int, default=8080,
                    help='port which to listen on')
parser.add_argument('--host', type=str, default='127.0.0.1',
                    help='hostname/ip which to listen on')
parser.add_argument('--twitter-auth-key', type=str,
                    default=TWITTER_AUTH_KEY, help='auth key for twitter guest session')
parser.add_argument('--cors-allow', type=str, default=None,
                    help='value for Access-Control-Allow-Origin header')
args = parser.parse_args()

TwitterSession.twitter_auth_key = args.twitter_auth_key

if (args.cors_allow is None):
    debug('[CORS] Running without CORS headers')
else:
    debug('[CORS] Allowing requests from: ' + args.cors_allow)


def run():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(login_guests())
    app = web.Application(loop=loop)
    app.add_routes(routes)
    web.run_app(app, host=args.host, port=9000)


if args.daemon:
    with daemon.DaemonContext():
        run()
else:
    run()
