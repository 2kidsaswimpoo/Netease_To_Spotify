"""Migrate followed artists and saved albums from Netease Music to Spotify.

Designed for Spotify Web API Development Mode after the February 2026 changes
and for the PyNCM version used by muyangye/Netease_To_Spotify.

Run this file from the Netease_To_Spotify project directory. It reads Spotify
credentials and the Netease browser cookie from config.yml.
"""

import argparse
import csv
import json
import os
import re
import time
from difflib import SequenceMatcher
from http.cookies import SimpleCookie
from pathlib import Path

import spotipy
import requests
import yaml
from pyncm import GetCurrentSession
from pyncm.apis import WeapiCryptoRequest
from spotipy.exceptions import SpotifyException
from tqdm import tqdm
from unidecode import unidecode


SPOTIFY_SCOPES = "user-library-modify user-follow-modify"
STATE_PATH = Path(".netease_library_migration_state.json")
UNMATCHED_PATH = Path("netease_library_unmatched.csv")
SPOTIFY_BATCH_SIZE = 40
SEARCH_DELAY_SECONDS = 0.35


@WeapiCryptoRequest
def get_netease_account():
    return "/weapi/w/nuser/account/get", {}


@WeapiCryptoRequest
def get_followed_artists(offset=0, limit=1000):
    return "/weapi/artist/sublist", {
        "offset": str(offset),
        "limit": str(limit),
        "total": "true",
    }


@WeapiCryptoRequest
def get_saved_albums(offset=0, limit=1000):
    return "/weapi/album/sublist", {
        "offset": str(offset),
        "limit": str(limit),
        "total": "true",
    }


def parse_cookie_header(raw_cookie):
    parsed = SimpleCookie()
    parsed.load(raw_cookie.strip())
    return {key: morsel.value for key, morsel in parsed.items()}


def configure_netease_session(raw_cookie):
    cookies = parse_cookie_header(raw_cookie)
    if not cookies.get("MUSIC_U") and not cookies.get("MUSIC_A"):
        raise ValueError(
            "Cookie 中没有 MUSIC_U 或 MUSIC_A；请复制登录后的完整网易云 Cookie。"
        )

    session = GetCurrentSession()
    session.cookies.update(cookies)
    session.csrf_token = cookies.get("__csrf", "")

    # PyNCM does not set a request timeout by default. Add one so an unavailable
    # Netease endpoint or proxy produces a useful error instead of hanging.
    original_request = session.request

    def request_with_timeout(method, url, *args, **kwargs):
        kwargs.setdefault("timeout", 20)
        return original_request(method, url, *args, **kwargs)

    session.request = request_with_timeout

    print("已从 config.yml 读取网易云 Cookie，正在验证登录……", flush=True)
    try:
        account = get_netease_account()
    except requests.RequestException as exc:
        raise RuntimeError(
            "网易云登录验证请求在 20 秒内未完成。请检查网络、代理和 Cookie。"
        ) from exc
    if not isinstance(account, dict) or account.get("code") != 200:
        raise RuntimeError(f"网易云 Cookie 验证失败：{account}")

    account_info = account.get("account") or {}
    profile = account.get("profile") or {}
    if not account_info and not profile:
        raise RuntimeError("网易云 Cookie 已失效，请重新登录网页并复制 Cookie。")

    user_id = account_info.get("id") or profile.get("userId")
    session.login_info = {
        "success": True,
        "tick": time.time(),
        "content": {"account": {"id": user_id}, "profile": profile},
    }
    nickname = profile.get("nickname", "当前用户")
    print(f"网易云登录验证成功：{nickname}")


def fetch_all(fetch_page, label):
    records = []
    offset = 0
    limit = 1000

    while True:
        response = fetch_page(offset=offset, limit=limit)
        if not isinstance(response, dict) or response.get("code") != 200:
            raise RuntimeError(f"读取网易云{label}失败：{response}")

        page = response.get("data") or []
        records.extend(page)
        if not page or response.get("hasMore") is False or len(page) < limit:
            break
        offset += len(page)

    print(f"读取到网易云{label}：{len(records)} 项")
    return records


def normalize(value):
    value = unidecode(value or "").lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def similarity(left, right):
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def artist_name_from_album(album):
    artists = album.get("artists") or []
    if artists:
        return artists[0].get("name", "")
    return (album.get("artist") or {}).get("name", "")


def source_key(kind, record):
    source_id = record.get("id")
    if source_id is not None:
        return f"{kind}:{source_id}"
    if kind == "artist":
        return f"artist-name:{normalize(record.get('name', ''))}"
    return (
        f"album-name:{normalize(record.get('name', ''))}:"
        f"{normalize(artist_name_from_album(record))}"
    )


class LibraryMigrator:
    def __init__(self, config_path="config.yml"):
        with open(config_path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)

        self.spotify = spotipy.Spotify(
            auth_manager=spotipy.oauth2.SpotifyOAuth(
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                redirect_uri=config["redirect_uri"],
                scope=SPOTIFY_SCOPES,
                cache_path=".cache-library",
            ),
            requests_timeout=15,
            retries=3,
            status_retries=3,
            backoff_factor=1,
        )
        self.state = self.load_state()

    @staticmethod
    def load_state():
        if not STATE_PATH.exists():
            return {"artist": [], "album": []}
        try:
            with open(STATE_PATH, encoding="utf-8") as state_file:
                state = json.load(state_file)
            state.setdefault("artist", [])
            state.setdefault("album", [])
            return state
        except (OSError, json.JSONDecodeError):
            print("警告：断点文件无法读取，将从头检查；重复保存是安全的。")
            return {"artist": [], "album": []}

    def save_state(self):
        temporary_path = STATE_PATH.with_suffix(".tmp")
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(self.state, state_file, ensure_ascii=False, indent=2)
        temporary_path.replace(STATE_PATH)

    @staticmethod
    def record_unmatched(kind, name, artist=""):
        needs_header = not UNMATCHED_PATH.exists()
        with open(UNMATCHED_PATH, "a", encoding="utf-8", newline="") as report:
            writer = csv.writer(report)
            if needs_header:
                writer.writerow(["type", "name", "artist"])
            writer.writerow([kind, name, artist])

    def spotify_search(self, query, item_type):
        response = self.spotify.search(q=query, limit=5, type=item_type)
        time.sleep(SEARCH_DELAY_SECONDS)
        return response[f"{item_type}s"]["items"]

    def match_artist(self, record):
        name = record.get("name", "").strip()
        if not name:
            return None

        candidates = self.spotify_search(f'artist:"{name}"', "artist")
        if not candidates:
            return None

        ranked = sorted(
            candidates,
            key=lambda item: similarity(name, item.get("name", "")),
            reverse=True,
        )
        best = ranked[0]
        if similarity(name, best.get("name", "")) < 0.82:
            return None
        return best.get("uri")

    def match_album(self, record):
        name = record.get("name", "").strip()
        artist = artist_name_from_album(record).strip()
        if not name:
            return None

        query = f'album:"{name}"'
        if artist:
            query += f' artist:"{artist}"'
        candidates = self.spotify_search(query, "album")
        if not candidates:
            return None

        def album_score(candidate):
            candidate_artists = candidate.get("artists") or []
            candidate_artist = (
                candidate_artists[0].get("name", "") if candidate_artists else ""
            )
            name_score = similarity(name, candidate.get("name", ""))
            artist_score = similarity(artist, candidate_artist) if artist else 1.0
            return (name_score * 0.7) + (artist_score * 0.3)

        ranked = sorted(candidates, key=album_score, reverse=True)
        best = ranked[0]
        if album_score(best) < 0.78:
            return None
        return best.get("uri")

    def save_uri_batch(self, pending):
        if not pending:
            return
        uris = [item[1] for item in pending]

        # Spotify 2026 Development Mode: PUT /me/library with Spotify URIs.
        # Spotify's generic library endpoint requires `uris` as a
        # comma-separated query parameter, not as a JSON request body.
        self.spotify._put("me/library", uris=",".join(uris))
        for key, _uri in pending:
            kind = key.split(":", 1)[0]
            if key not in self.state[kind]:
                self.state[kind].append(key)
        self.save_state()

    def migrate_records(self, kind, records):
        completed = set(self.state[kind])
        remaining = [record for record in records if source_key(kind, record) not in completed]
        if not remaining:
            print(f"{kind}：没有尚未迁移的项目。")
            return

        pending = []
        matched = 0
        unmatched = 0
        label = "关注歌手" if kind == "artist" else "收藏专辑"

        for record in tqdm(remaining, desc=label):
            key = source_key(kind, record)
            name = record.get("name", "")
            artist = artist_name_from_album(record) if kind == "album" else ""

            try:
                uri = (
                    self.match_artist(record)
                    if kind == "artist"
                    else self.match_album(record)
                )
            except SpotifyException:
                # Pending matches have not yet been marked complete, so the next
                # run safely retries them.
                self.save_uri_batch(pending)
                raise

            if uri:
                pending.append((key, uri))
                matched += 1
                if len(pending) >= SPOTIFY_BATCH_SIZE:
                    self.save_uri_batch(pending)
                    pending = []
            else:
                unmatched += 1
                self.record_unmatched(kind, name, artist)
                if key not in self.state[kind]:
                    self.state[kind].append(key)
                self.save_state()

        self.save_uri_batch(pending)
        print(f"{label}完成：匹配 {matched}，未匹配 {unmatched}")


def main():
    parser = argparse.ArgumentParser(
        description="迁移网易云关注歌手和收藏专辑到 Spotify（2026 API）"
    )
    parser.add_argument("--artists", action="store_true", help="仅迁移关注歌手")
    parser.add_argument("--albums", action="store_true", help="仅迁移收藏专辑")
    parser.add_argument(
        "--config", default="config.yml", help="Spotify 配置文件路径"
    )
    args = parser.parse_args()

    migrate_artists = args.artists or not (args.artists or args.albums)
    migrate_albums = args.albums or not (args.artists or args.albums)

    with open(args.config, encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    # Environment variable remains available as a temporary override, but the
    # normal non-interactive path is the netease_cookie field in config.yml.
    raw_cookie = os.environ.get("NETEASE_COOKIE") or config.get("netease_cookie")
    if not raw_cookie or raw_cookie == "YOUR_NETEASE_COOKIE_HERE":
        raise RuntimeError(
            "config.yml 中缺少 netease_cookie。请添加：\n"
            "netease_cookie: '你的完整网易云 Cookie'"
        )
    configure_netease_session(raw_cookie)

    migrator = LibraryMigrator(args.config)
    if migrate_artists:
        artists = fetch_all(get_followed_artists, "关注歌手")
        migrator.migrate_records("artist", artists)
    if migrate_albums:
        albums = fetch_all(get_saved_albums, "收藏专辑")
        migrator.migrate_records("album", albums)

    print(f"断点记录：{STATE_PATH.resolve()}")
    if UNMATCHED_PATH.exists():
        print(f"未匹配报告：{UNMATCHED_PATH.resolve()}")


if __name__ == "__main__":
    main()
