"""Safely remove every saved album from the current Spotify account.

The script always creates a CSV backup first, requires an explicit typed
confirmation, deletes through Spotify's 2026 DELETE /me/library endpoint in
batches of 40 album URIs, and checkpoints after every successful batch.
"""

import csv
import json
import time
from datetime import datetime
from pathlib import Path

import requests
import spotipy
import urllib3
import yaml
from spotipy.exceptions import SpotifyException


SPOTIFY_SCOPES = "user-library-read user-library-modify"
BATCH_SIZE = 40
CONFIRMATION_TEXT = "确认清空全部收藏专辑"
STATE_PATH = Path(".clear_spotify_saved_albums_state.json")
DEFAULT_RATE_LIMIT_WAIT = 60


class SavedAlbumCleaner:
    def __init__(self, config_path="config.yml"):
        with open(config_path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)

        # Handle 429 responses ourselves so Spotify never appears to hang
        # silently inside urllib3/Spotipy.
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
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                redirect_uri=config["redirect_uri"],
                scope=SPOTIFY_SCOPES,
                cache_path=".cache-clear-albums-2026",
            ),
            requests_session=session,
            requests_timeout=20,
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
                        "Spotify Development Mode 配额已用完。删除进度已经保存；"
                        "配额恢复后重新运行本脚本即可续传。"
                    ) from exc

                raw_wait = exc.headers.get("Retry-After", DEFAULT_RATE_LIMIT_WAIT)
                try:
                    wait_seconds = max(1, int(float(raw_wait)))
                except (TypeError, ValueError):
                    wait_seconds = DEFAULT_RATE_LIMIT_WAIT

                print(
                    f"\nSpotify 触发短期限流（{description}），等待 "
                    f"{wait_seconds} 秒后自动继续。"
                )
                while wait_seconds > 0:
                    print(f"剩余等待：{wait_seconds} 秒   ", end="\r", flush=True)
                    step = min(10, wait_seconds)
                    time.sleep(step)
                    wait_seconds -= step
                print("限流等待结束，正在重试。                    ")

    def get_all_saved_albums(self):
        albums = []
        offset = 0

        while True:
            page = self.spotify_call(
                lambda: self.spotify._get(
                    "me/albums", limit=50, offset=offset
                ),
                "读取收藏专辑",
            )
            items = page.get("items") or []
            albums.extend(items)
            if not page.get("next") or not items:
                break
            offset += len(items)

        return albums

    @staticmethod
    def export_backup(albums):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = Path(f"spotify_saved_albums_backup_{timestamp}.csv")

        with open(backup_path, "w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(
                [
                    "added_at",
                    "album_name",
                    "artists",
                    "release_date",
                    "spotify_uri",
                    "spotify_url",
                ]
            )
            for saved_item in albums:
                album = saved_item.get("album") or {}
                artists = ", ".join(
                    artist.get("name", "")
                    for artist in album.get("artists") or []
                )
                writer.writerow(
                    [
                        saved_item.get("added_at", ""),
                        album.get("name", ""),
                        artists,
                        album.get("release_date", ""),
                        album.get("uri", ""),
                        (album.get("external_urls") or {}).get("spotify", ""),
                    ]
                )

        return backup_path

    @staticmethod
    def load_incomplete_state():
        if not STATE_PATH.exists():
            return None
        try:
            with open(STATE_PATH, encoding="utf-8") as state_file:
                state = json.load(state_file)
            if state.get("status") == "in_progress":
                return state
        except (OSError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def save_state(state):
        temporary_path = STATE_PATH.with_suffix(".tmp")
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2)
        temporary_path.replace(STATE_PATH)

    def remove_batch(self, uris):
        self.spotify_call(
            lambda: self.spotify._delete(
                "me/library", uris=",".join(uris)
            ),
            f"移除 {len(uris)} 张收藏专辑",
        )

    def run(self):
        state = self.load_incomplete_state()

        if state:
            all_uris = state["album_uris"]
            next_index = int(state.get("next_index", 0))
            backup_path = Path(state["backup_path"])
            print("发现上次未完成的清空任务。")
            print(f"CSV 备份：{backup_path.resolve()}")
            print(f"总数：{len(all_uris)}，已处理：{next_index}，剩余：{len(all_uris) - next_index}")
        else:
            print("正在读取 Spotify 收藏专辑……", flush=True)
            albums = self.get_all_saved_albums()
            if not albums:
                print("Spotify 收藏专辑已经是空的，无需删除。")
                return

            backup_path = self.export_backup(albums)
            all_uris = [
                (item.get("album") or {}).get("uri")
                for item in albums
                if (item.get("album") or {}).get("uri")
            ]
            next_index = 0
            print(f"共读取到 {len(all_uris)} 张收藏专辑。")
            print(f"CSV 备份已保存：{backup_path.resolve()}")

        print("\n此操作会取消收藏上述全部专辑，但不会删除歌单或取消关注歌手。")
        typed = input(f"如果确定继续，请完整输入“{CONFIRMATION_TEXT}”：").strip()
        if typed != CONFIRMATION_TEXT:
            print("确认文字不匹配，未删除任何专辑。")
            return

        state = {
            "status": "in_progress",
            "backup_path": str(backup_path),
            "album_uris": all_uris,
            "next_index": next_index,
        }
        self.save_state(state)

        while next_index < len(all_uris):
            batch = all_uris[next_index : next_index + BATCH_SIZE]
            self.remove_batch(batch)
            next_index += len(batch)
            state["next_index"] = next_index
            self.save_state(state)
            print(f"删除进度：{next_index}/{len(all_uris)}")

        remaining = self.get_all_saved_albums()
        state["status"] = "complete"
        state["remaining_after_verification"] = len(remaining)
        self.save_state(state)

        if remaining:
            print(f"操作结束，但验证时仍发现 {len(remaining)} 张收藏专辑。请重新运行检查。")
        else:
            print("完成：Spotify 收藏专辑已清空。")
        print(f"CSV 备份保留在：{backup_path.resolve()}")


def main():
    cleaner = SavedAlbumCleaner("config.yml")
    cleaner.run()


if __name__ == "__main__":
    main()
