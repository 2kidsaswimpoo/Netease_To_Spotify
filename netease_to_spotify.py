import base64
import re
import sys
from datetime import date, datetime

import requests
import spotipy
import yaml
from pyncm import apis
from tqdm import tqdm
from unidecode import unidecode


DEFAULT_COVER_PATH = "assets/netease.png"
SPOTIFY_SCOPES = (
    "playlist-modify-public playlist-modify-private "
    "user-library-read ugc-image-upload"
)

# Spotify Web API paths used directly because Spotipy 2.23.0 still calls the
# playlist endpoints that were removed for Development Mode in February 2026.
CREATE_PLAYLIST_PATH = "me/playlists"
PLAYLIST_ITEMS_PATH = "playlists/{playlist_id}/items"

# Netease occasionally returns an invalid publishTime (for example, a date in
# year 2240). Invalid dates are excluded from the Spotify year filter.
UNIX_START = 1000  # milliseconds
MS_PER_S = 1000
NEXT_YEAR = datetime(datetime.now().year + 1, 1, 1).timestamp() * MS_PER_S


class NeteaseToSpotify:
    def __init__(self):
        print("---------- Starting Application ----------")
        with open("config.yml", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        try:
            self.spotify = spotipy.Spotify(
                auth_manager=spotipy.oauth2.SpotifyOAuth(
                    client_id=config["client_id"],
                    client_secret=config["client_secret"],
                    redirect_uri=config["redirect_uri"],
                    scope=SPOTIFY_SCOPES,
                )
            )
        except Exception as exc:
            print(f"Spotify authorization failed, program terminated: {exc}")
            sys.exit(1)

        self.cover_image_path = config["cover_image_path"]
        self.netease_playlist_id = config["netease_playlist_id"]

    def migrate(self, netease_playlist_ids=None):
        """Migrate one or more Netease playlists to Spotify."""
        if netease_playlist_ids is None:
            netease_playlist_ids = [self.netease_playlist_id]
        for netease_playlist_id in netease_playlist_ids:
            self.migrate_playlist(netease_playlist_id)

    def migrate_playlist(self, netease_playlist_id):
        print(
            "---------- Getting Netease Cloud Music Data With Id: "
            f"{netease_playlist_id} (this may take a few seconds) ----------"
        )
        netease_playlist = apis.playlist.GetPlaylistInfo(netease_playlist_id)
        netease_playlist_tracks = (
            self.get_netease_playlist_tracks_name_and_artist(netease_playlist)
        )

        cover_image_path = (
            netease_playlist["playlist"]["coverImgUrl"]
            if self.cover_image_path
            == "DESIRED_SPOTIFY_PLAYLIST_COVER_IMAGE_PATH"
            else self.cover_image_path
        )

        spotify_playlist_id = self.create_playlist(
            netease_playlist["playlist"]["name"], cover_image_path
        )

        print("---------- Inserting Songs to Spotify ----------")
        for name, artist, year in tqdm(netease_playlist_tracks):
            # Parenthetical text often makes Spotify search too restrictive.
            trimmed_name = re.sub(r"\(.*\)", "", name)
            try:
                track_uri = self.search_for_track(year, trimmed_name, artist)
            except (IndexError, KeyError, TypeError):
                print(
                    "Spotify could not find this song: "
                    f"{unidecode(name)}, {unidecode(artist)}"
                )
                continue

            # Do not swallow authorization, rate-limit, or endpoint errors as
            # false "song not found" messages.
            self.add_items_to_playlist(spotify_playlist_id, [track_uri])

    def create_playlist(self, name, cover_image_path):
        """Create a public playlist and return its Spotify ID."""
        print(
            f"Creating spotify playlist {name} "
            f"with cover image path {cover_image_path}"
        )

        # 2026 endpoint: POST /me/playlists.
        # Spotipy 2.23.0's user_playlist_create() uses the removed
        # POST /users/{user_id}/playlists endpoint, so call _post directly.
        playlist_id = self.spotify._post(
            CREATE_PLAYLIST_PATH,
            payload={"name": name, "public": True},
        )["id"]

        try:
            if cover_image_path.startswith("http"):
                b64_cover_image = self.get_base64_from_image_url(cover_image_path)
            else:
                b64_cover_image = self.get_base64_from_image_file(cover_image_path)

            if len(b64_cover_image.encode("ascii")) / 1024 > 256:
                print("The cover image is too large, using default cover image")
                b64_cover_image = self.get_base64_from_image_file(
                    DEFAULT_COVER_PATH
                )

            # PUT /playlists/{playlist_id}/images remains supported in 2026.
            self.spotify.playlist_upload_cover_image(
                playlist_id, b64_cover_image
            )
        except Exception as exc:
            # The playlist already exists at this point, so a cover failure
            # should not abort the song migration.
            print(f"Failed to upload Spotify playlist cover: {exc}")

        return playlist_id

    def add_items_to_playlist(self, playlist_id, items):
        """Add track/episode URIs through the 2026 playlist items endpoint."""
        uris = []
        for item in items:
            if item.startswith("spotify:"):
                uris.append(item)
            else:
                # Preserve compatibility if a caller passes an old-style ID.
                uris.append(f"spotify:track:{item}")

        # 2026 endpoint: POST /playlists/{playlist_id}/items.
        # Spotipy 2.23.0's playlist_add_items() still posts to /tracks.
        return self.spotify._post(
            PLAYLIST_ITEMS_PATH.format(playlist_id=playlist_id),
            payload={"uris": uris},
        )

    def get_base64_from_image_url(self, path):
        """Return the base64 representation of an image downloaded by URL."""
        response = requests.get(path, timeout=30)
        response.raise_for_status()
        return base64.b64encode(response.content).decode("utf-8")

    def get_base64_from_image_file(self, path):
        """Return the base64 representation of a local image."""
        with open(path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    def search_for_track(self, year, name, artist=None):
        """Search for a track and return its Spotify URI."""
        query = ""
        if year != -1:
            query += f"year:{year - 1}-{year + 1} "
        query += name
        if artist:
            query += " " + artist

        # The 2026 Development Mode maximum search limit is 10; limit=1 is
        # intentionally retained because the original matcher uses the first
        # (most relevant) result only.
        result = self.spotify.search(query, limit=1, type="track")
        return result["tracks"]["items"][0]["uri"]

    def get_netease_playlist_tracks_name_and_artist(self, playlist):
        """Return (name, first artist, year) tuples in playlist order."""
        track_ids = [
            track_id["id"] for track_id in playlist["playlist"]["trackIds"]
        ]
        songs = []

        # Split into chunks of at most 1000 for the PyNCM API.
        left, right = 0, 0
        while right < len(track_ids):
            right = left + min(1000, len(track_ids) - right)
            songs.extend(
                apis.track.GetTrackDetail(track_ids[left:right])["songs"]
            )
            left = right

        return [
            (
                song["name"],
                song["ar"][0]["name"],
                date.fromtimestamp(song["publishTime"] / MS_PER_S).year
                if "publishTime" in song
                and UNIX_START <= song["publishTime"] <= NEXT_YEAR
                else -1,
            )
            for song in songs
        ]
