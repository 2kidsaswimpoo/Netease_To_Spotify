"""Resume and batch a Netease-to-Spotify playlist migration.

This is a companion for muyangye/Netease_To_Spotify, adapted for Spotify's
2026 Development Mode API. It can reuse an existing Spotify playlist, resume
from a Netease source index, add matched tracks in batches, and checkpoint only
after a batch has been saved successfully.
"""

import argparse
import csv
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
import spotipy
import urllib3
import yaml
from pyncm import apis
from spotipy.exceptions import SpotifyException
from tqdm import tqdm
from unidecode import unidecode


SPOTIFY_SCOPES = (
    "playlist-modify-public playlist-modify-private playlist-read-private"
)
SOURCE_BATCH_SIZE = 50
SEARCH_DELAY_SECONDS = 0.60
DEFAULT_RATE_LIMIT_WAIT = 60


def normalize(value):
    value = unidecode(value or "").lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def similarity(left, right):
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def parse_spotify_playlist_id(value):
    if not value:
        return None
    value = value.strip()
    match = re.search(r"(?:playlist/|spotify:playlist:)([0-9A-Za-z]+)", value)
    return match.group(1) if match else value


class ResumablePlaylistMigrator:
    def __init__(self, config_path):
        with open(config_path, encoding="utf-8") as config_file:
            self.config = yaml.safe_load(config_file)

        # Disable urllib3's silent Retry-After sleeping. We handle HTTP 429
        # explicitly so the terminal always explains why it is waiting.
        session = requests.Session()
        retry = urllib3.Retry(
            total=0,
            connect=0,
            read=0,
            status=0,
            raise_on_status=False,
            respect_retry_after_header=False,
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        self.spotify = spotipy.Spotify(
            auth_manager=spotipy.oauth2.SpotifyOAuth(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scope=SPOTIFY_SCOPES,
                # Use a separate cache so an older token without
                # playlist-read-private cannot be reused accidentally.
                cache_path=".cache-resume-2026",
            ),
            requests_session=session,
            requests_timeout=20,
        )
        self.netease_playlist_id = str(self.config["netease_playlist_id"])
        self.state_path = Path(
            f".netease_playlist_{self.netease_playlist_id}_migration_state.json"
        )
        self.unmatched_path = Path(
            f"netease_playlist_{self.netease_playlist_id}_unmatched.csv"
        )

    def spotify_call(self, operation, description):
        while True:
            try:
                return operation()
            except SpotifyException as exc:
                if exc.http_status != 429:
                    raise
                quota_exceeded = (
                    exc.reason == "QUOTA_EXCEEDED"
                    or "QUOTA_EXCEEDED" in str(exc)
                )
                if quota_exceeded:
                    raise RuntimeError(
                        "Spotify Development Mode 的账号共享配额已经用完。"
                        "当前批次尚未写入断点，稍后重新运行会从上一个安全断点继续；"
                        "这不是短时间等待可以解除的 30 秒滚动限流。"
                    ) from exc
                raw_wait = exc.headers.get("Retry-After", DEFAULT_RATE_LIMIT_WAIT)
                try:
                    wait_seconds = max(1, int(float(raw_wait)))
                except (TypeError, ValueError):
                    wait_seconds = DEFAULT_RATE_LIMIT_WAIT
                print(
                    f"\nSpotify 触发限流（{description}），服务器要求等待 "
                    f"{wait_seconds} 秒；倒计时结束后自动继续。"
                )
                while wait_seconds > 0:
                    print(f"剩余等待：{wait_seconds} 秒   ", end="\r", flush=True)
                    step = min(10, wait_seconds)
                    time.sleep(step)
                    wait_seconds -= step
                print("限流等待结束，正在重试。                    ")

    def fetch_netease_tracks(self):
        print(f"正在读取网易云歌单 {self.netease_playlist_id}……")
        playlist = apis.playlist.GetPlaylistInfo(self.netease_playlist_id)
        if "playlist" not in playlist:
            raise RuntimeError(f"网易云歌单读取失败：{playlist}")

        track_ids = [item["id"] for item in playlist["playlist"]["trackIds"]]
        songs = []
        for left in range(0, len(track_ids), 1000):
            response = apis.track.GetTrackDetail(track_ids[left : left + 1000])
            songs.extend(response.get("songs") or [])

        tracks = []
        for song in songs:
            artists = song.get("ar") or []
            tracks.append(
                {
                    "name": song.get("name", ""),
                    "artist": artists[0].get("name", "") if artists else "",
                }
            )
        return playlist["playlist"], tracks

    def load_state(self):
        if not self.state_path.exists():
            return None
        try:
            with open(self.state_path, encoding="utf-8") as state_file:
                return json.load(state_file)
        except (OSError, json.JSONDecodeError):
            print("警告：旧断点文件不可读，将使用命令行指定的续传位置。")
            return None

    def save_state(self, playlist_id, next_index):
        state = {
            "netease_playlist_id": self.netease_playlist_id,
            "spotify_playlist_id": playlist_id,
            "next_source_index": next_index,
        }
        temporary_path = self.state_path.with_suffix(".tmp")
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2)
        temporary_path.replace(self.state_path)

    def current_playlists(self):
        playlists = []
        offset = 0
        while True:
            page = self.spotify_call(
                lambda: self.spotify._get(
                    "me/playlists", limit=50, offset=offset
                ),
                "读取当前用户歌单",
            )
            items = page.get("items") or []
            playlists.extend(items)
            if not page.get("next") or not items:
                break
            offset += len(items)
        return playlists

    @staticmethod
    def playlist_item_total(playlist):
        container = playlist.get("items") or playlist.get("tracks") or {}
        return container.get("total") or 0

    def find_existing_playlist(self, name):
        candidates = [
            playlist
            for playlist in self.current_playlists()
            if playlist.get("name") == name
        ]
        if not candidates:
            return None
        # If previous attempts created duplicates, prefer the same-name playlist
        # that already contains the most items.
        candidates.sort(key=self.playlist_item_total, reverse=True)
        chosen = candidates[0]
        print(
            "自动选择已有 Spotify 歌单："
            f"{chosen.get('name')}（当前 {self.playlist_item_total(chosen)} 项，"
            f"ID {chosen.get('id')}）"
        )
        return chosen.get("id")

    def create_playlist(self, name):
        response = self.spotify_call(
            lambda: self.spotify._post(
                "me/playlists", payload={"name": name, "public": True}
            ),
            "创建歌单",
        )
        return response["id"]

    def search_track(self, track):
        name = track["name"].strip()
        artist = track["artist"].strip()
        query = f'track:"{name}"'
        if artist:
            query += f' artist:"{artist}"'

        response = self.spotify_call(
            lambda: self.spotify.search(q=query, limit=3, type="track"),
            f"搜索 {name}",
        )
        time.sleep(SEARCH_DELAY_SECONDS)
        candidates = response.get("tracks", {}).get("items") or []
        if not candidates:
            return None

        def score(candidate):
            candidate_artists = candidate.get("artists") or []
            candidate_artist = (
                candidate_artists[0].get("name", "") if candidate_artists else ""
            )
            return (
                similarity(name, candidate.get("name", "")) * 0.72
                + similarity(artist, candidate_artist) * 0.28
            )

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        return best.get("uri") if score(best) >= 0.76 else None

    def add_items(self, playlist_id, uris):
        if not uris:
            return
        self.spotify_call(
            lambda: self.spotify._post(
                f"playlists/{playlist_id}/items", payload={"uris": uris}
            ),
            f"批量添加 {len(uris)} 首歌曲",
        )

    def record_unmatched(self, index, track):
        needs_header = not self.unmatched_path.exists()
        with open(self.unmatched_path, "a", encoding="utf-8", newline="") as report:
            writer = csv.writer(report)
            if needs_header:
                writer.writerow(["source_index", "name", "artist"])
            writer.writerow([index, track["name"], track["artist"]])

    def migrate(self, resume_index=0, playlist_id=None):
        netease_playlist, tracks = self.fetch_netease_tracks()
        playlist_name = netease_playlist["name"]
        state = self.load_state()

        if state:
            playlist_id = state.get("spotify_playlist_id") or playlist_id
            resume_index = int(state.get("next_source_index", resume_index))
            print(f"读取到断点，将从 {resume_index}/{len(tracks)} 继续。")

        playlist_id = parse_spotify_playlist_id(playlist_id)
        if not playlist_id and resume_index > 0:
            playlist_id = self.find_existing_playlist(playlist_name)
        if not playlist_id:
            if resume_index > 0:
                raise RuntimeError(
                    "没有找到已有同名 Spotify 歌单。请通过 --playlist-id 提供歌单链接。"
                )
            playlist_id = self.create_playlist(playlist_name)

        if not 0 <= resume_index <= len(tracks):
            raise ValueError(
                f"续传位置必须在 0 到 {len(tracks)} 之间，当前为 {resume_index}。"
            )

        self.save_state(playlist_id, resume_index)
        print(
            f"开始迁移：网易云共 {len(tracks)} 首，从第 {resume_index + 1} 首继续；"
            f"Spotify 歌单 ID：{playlist_id}"
        )

        index = resume_index
        with tqdm(total=len(tracks), initial=index, desc="迁移进度") as progress:
            while index < len(tracks):
                batch_end = min(index + SOURCE_BATCH_SIZE, len(tracks))
                uris = []

                for source_index in range(index, batch_end):
                    track = tracks[source_index]
                    uri = self.search_track(track)
                    if uri:
                        uris.append(uri)
                    else:
                        self.record_unmatched(source_index, track)

                # Checkpoint advances only after this entire source batch has
                # been added successfully, so Ctrl+C never skips unsaved tracks.
                self.add_items(playlist_id, uris)
                index = batch_end
                self.save_state(playlist_id, index)
                progress.update(batch_end - progress.n)

        print("迁移完成。")
        print(f"断点文件：{self.state_path.resolve()}")
        if self.unmatched_path.exists():
            print(f"未匹配报告：{self.unmatched_path.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Spotify 2026 兼容的网易云歌单批量续传工具"
    )
    parser.add_argument(
        "--resume-index",
        type=int,
        default=0,
        help="已经处理完成的网易云歌曲数量，例如 694",
    )
    parser.add_argument(
        "--playlist-id",
        help="已有 Spotify 歌单 ID、链接或 spotify:playlist: URI",
    )
    parser.add_argument("--config", default="config.yml")
    args = parser.parse_args()

    migrator = ResumablePlaylistMigrator(args.config)
    migrator.migrate(args.resume_index, args.playlist_id)


if __name__ == "__main__":
    main()
