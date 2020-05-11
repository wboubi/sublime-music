import logging
import shutil
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from sublime import util
from sublime.adapters import api_objects as API

from . import models
from .. import CacheMissError, CachingAdapter, ConfigParamDescriptor, SongCacheStatus


class FilesystemAdapter(CachingAdapter):
    """
    Defines an adapter which retrieves its data from the local filesystem.
    """

    # Configuration and Initialization Properties
    # ==================================================================================
    @staticmethod
    def get_config_parameters() -> Dict[str, ConfigParamDescriptor]:
        return {
            # TODO: download on play?
        }

    @staticmethod
    def verify_configuration(config: Dict[str, Any]) -> Dict[str, Optional[str]]:
        return {}

    def __init__(
        self, config: dict, data_directory: Path, is_cache: bool = False,
    ):
        self.data_directory = data_directory
        self.cover_art_dir = self.data_directory.joinpath("cover_art")
        self.music_dir = self.data_directory.joinpath("music")

        self.cover_art_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)

        self.is_cache = is_cache

        self.db_write_lock: threading.Lock = threading.Lock()
        database_filename = data_directory.joinpath("cache.db")
        models.database.init(database_filename)
        models.database.connect()

        with self.db_write_lock, models.database.atomic():
            models.database.create_tables(models.ALL_TABLES)

    def shutdown(self):
        logging.info("Shutdown complete")

    # Usage and Availability Properties
    # ==================================================================================
    can_be_cached = False  # Can't be cached (there's no need).
    can_service_requests = True  # Can always be used to service requests.
    can_get_playlists = True
    can_get_playlist_details = True
    can_get_cover_art_uri = True
    can_get_song_uri = True
    can_get_song_details = True
    can_get_genres = True

    supported_schemes = ("file",)

    # Data Helper Methods
    # ==================================================================================

    # Data Retrieval Methods
    # ==================================================================================
    def get_cached_status(self, song: API.Song) -> SongCacheStatus:
        song = models.Song.get_or_none(models.Song.id == song.id)
        if not song:
            return SongCacheStatus.NOT_CACHED
        cache_path = self.music_dir.joinpath(song.path)
        if cache_path.exists():
            # TODO check if path is permanently cached
            return SongCacheStatus.CACHED

        return SongCacheStatus.NOT_CACHED

    def get_playlists(self) -> Sequence[API.Playlist]:
        playlists = list(models.Playlist.select())
        if self.is_cache:
            # Determine if the adapter has ingested data for get_playlists before, and
            # if not, cache miss.
            cache_key = CachingAdapter.CachedDataKey.PLAYLISTS
            if not models.CacheInfo.get_or_none(
                models.CacheInfo.cache_key == cache_key
            ):
                raise CacheMissError(partial_data=playlists)
        return playlists

    def get_playlist_details(self, playlist_id: str) -> API.PlaylistDetails:
        playlist = models.Playlist.get_or_none(models.Playlist.id == playlist_id)

        # Handle the case that this is the ground truth adapter.
        if not self.is_cache:
            if not playlist:
                raise Exception(f"Playlist {playlist_id} does not exist.")
            return playlist

        # If we haven't ingested data for this playlist before, raise a CacheMissError
        # with the partial playlist data.
        cache_key = CachingAdapter.CachedDataKey.PLAYLIST_DETAILS
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == cache_key,
            models.CacheInfo.params_hash == util.params_hash(playlist_id),
        )
        if not cache_info:
            raise CacheMissError(partial_data=playlist)

        return playlist

    def get_cover_art_uri(self, cover_art_id: str, scheme: str) -> str:
        # TODO cache by the content of the file (need to see if cover art ID is
        # duplicated a lot)?
        params_hash = util.params_hash(cover_art_id)
        cover_art_filename = self.cover_art_dir.joinpath(params_hash)

        # Handle the case that this is the ground truth adapter.
        if not self.is_cache:
            if not cover_art_filename.exists:
                raise Exception(f"Cover Art for {cover_art_id} does not exist.")
            return str(cover_art_filename)

        if not cover_art_filename.exists():
            raise CacheMissError()

        cache_key = CachingAdapter.CachedDataKey.COVER_ART_FILE
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == cache_key,
            models.CacheInfo.params_hash == params_hash,
        )
        if not cache_info:
            raise CacheMissError(partial_data=str(cover_art_filename))

        return str(cover_art_filename)

    def get_song_uri(self, song_id: str, scheme: str, stream: bool = False) -> str:
        song = models.Song.get_or_none(models.Song.id == song_id)
        if not song:
            if self.is_cache:
                raise CacheMissError()
            else:
                raise Exception(f"Song {song_id} does not exist.")

        music_filename = self.music_dir.joinpath(song.path)

        # Handle the case that this is the ground truth adapter.
        if not self.is_cache:
            if not music_filename.exists:
                raise Exception(f"Music File for song {song_id} does not exist.")
            return str(music_filename)

        if not music_filename.exists():
            raise CacheMissError()

        cache_key = CachingAdapter.CachedDataKey.SONG_FILE
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == cache_key,
            models.CacheInfo.params_hash == util.params_hash(song_id),
        )
        if not cache_info:
            raise CacheMissError(partial_data=str(music_filename))

        return str(music_filename)

    def get_song_details(self, song_id: str) -> API.Song:
        song = models.Song.get_or_none(models.Song.id == song_id)

        # Handle the case that this is the ground truth adapter.
        if not self.is_cache:
            if not song:
                raise Exception(f"song {song} does not exist.")
            return song

        # If we haven't ingested data for this playlist before, or it's been
        # invalidated, raise a CacheMissError with the partial song data.
        cache_key = CachingAdapter.CachedDataKey.SONG_DETAILS
        cache_info = models.CacheInfo.get_or_none(
            models.CacheInfo.cache_key == cache_key,
            models.CacheInfo.params_hash == util.params_hash(song_id),
        )
        if not cache_info:
            raise CacheMissError(partial_data=song)

        return song

    def get_genres(self) -> Sequence[API.Genre]:
        genres = list(models.Genre.select().order_by(models.Genre.name))
        if self.is_cache:
            # Determine if the adapter has ingested data for get_playlists before, and
            # if not, cache miss.
            cache_key = CachingAdapter.CachedDataKey.GENRES
            if not models.CacheInfo.get_or_none(
                models.CacheInfo.cache_key == cache_key
            ):
                raise CacheMissError(partial_data=genres)
        return genres

    # Data Ingestion Methods
    # ==================================================================================
    def ingest_new_data(
        self,
        data_key: CachingAdapter.CachedDataKey,
        params: Tuple[Any, ...],
        data: Any,
    ):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_ingest_new_data(data_key, params, data)

    def invalidate_data(
        self, function: CachingAdapter.CachedDataKey, params: Tuple[Any, ...]
    ):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_invalidate_data(function, params)

    def delete_data(
        self, function: CachingAdapter.CachedDataKey, params: Tuple[Any, ...]
    ):
        assert self.is_cache, "FilesystemAdapter is not in cache mode!"

        # Wrap the actual ingestion function in a database lock, and an atomic
        # transaction.
        with self.db_write_lock, models.database.atomic():
            self._do_delete_data(function, params)

    def _do_ingest_new_data(
        self,
        data_key: CachingAdapter.CachedDataKey,
        params: Tuple[Any, ...],
        data: Any,
    ):
        # TODO may need to remove reliance on asdict in order to support more backends.
        params_hash = util.params_hash(*params)
        models.CacheInfo.insert(
            cache_key=data_key,
            params_hash=params_hash,
            last_ingestion_time=datetime.now(),
        ).on_conflict_replace().execute()

        def ingest_directory_data(api_directory: API.Directory) -> models.Directory:
            directory_data = asdict(api_directory)
            directory, created = models.Directory.get_or_create(
                id=api_directory.id, defaults=directory_data
            )

            if not created:
                for k, v in directory_data.items():
                    setattr(directory, k, v)
                directory.save()

            return directory

        def ingest_genre_data(api_genre: API.Genre) -> models.Genre:
            genre_data = asdict(api_genre)
            genre, created = models.Genre.get_or_create(
                name=api_genre.name, defaults=asdict(api_genre)
            )

            if not created:
                for k, v in genre_data.items():
                    setattr(genre, k, v)
                genre.save()

            return genre

        def ingest_album_data(api_album: API.Album) -> models.Album:
            album_data = asdict(api_album)
            album, created = models.Album.get_or_create(
                id=api_album.id, defaults=asdict(api_album)
            )

            if not created:
                for k, v in album_data.items():
                    setattr(album, k, v)
                album.save()

            return album

        def ingest_artist_data(api_artist: API.Artist) -> models.Artist:
            artist_data = asdict(api_artist)
            artist, created = models.Artist.get_or_create(
                id=api_artist.id, defaults=artist_data
            )

            if not created:
                for k, v in artist_data.items():
                    setattr(artist, k, v)
                artist.save()

            return artist

        def ingest_song_data(api_song: API.Song) -> models.Song:
            song_data = {
                **asdict(api_song),
                # Deal with foreign key fields
                "album": ingest_album_data(al) if (al := api_song.album) else None,
                "artist": ingest_artist_data(ar) if (ar := api_song.artist) else None,
                "genre": ingest_genre_data(g) if (g := api_song.genre) else None,
                "parent": ingest_directory_data(d) if (d := api_song.parent) else None,
            }

            song, created = models.Song.get_or_create(
                id=song_data["id"], defaults=song_data
            )

            if not created:
                for k, v in song_data.items():
                    setattr(song, k, v)
                song.save()

            return song

        if data_key == CachingAdapter.CachedDataKey.PLAYLISTS:
            models.Playlist.insert_many(
                map(asdict, data)
            ).on_conflict_replace().execute()
            models.Playlist.delete().where(
                models.Playlist.id.not_in([p.id for p in data])
            ).execute()

        elif data_key == CachingAdapter.CachedDataKey.PLAYLIST_DETAILS:
            song_objects = [ingest_song_data(s) for s in data.songs]
            playlist_data = {**asdict(data), "songs": song_objects}
            playlist, playlist_created = models.Playlist.get_or_create(
                id=playlist_data["id"], defaults=playlist_data
            )

            # Update the values if the playlist already existed.
            if not playlist_created:
                for k, v in playlist_data.items():
                    setattr(playlist, k, v)

                playlist.save()

        elif data_key == CachingAdapter.CachedDataKey.COVER_ART_FILE:
            # ``data`` is the filename of the tempfile in this case
            shutil.copy(str(data), str(self.cover_art_dir.joinpath(params_hash)))

        elif data_key == CachingAdapter.CachedDataKey.SONG_FILE:
            relative_path = models.Song.get_by_id(params[0]).path
            absolute_path = self.music_dir.joinpath(relative_path)
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(data), str(absolute_path))

        elif data_key == CachingAdapter.CachedDataKey.SONG_DETAILS:
            ingest_song_data(data)

        elif data_key == CachingAdapter.CachedDataKey.GENRES:
            models.Genre.insert_many(map(asdict, data)).on_conflict_replace().execute()
            models.Genre.delete().where(
                models.Genre.name.not_in([g.name for g in data])
            ).execute()

    def _invalidate_cover_art(self, cover_art_id: str):
        models.CacheInfo.delete().where(
            models.CacheInfo.cache_key == CachingAdapter.CachedDataKey.COVER_ART_FILE,
            models.CacheInfo.params_hash == util.params_hash(cover_art_id),
        ).execute()

    def _do_invalidate_data(
        self, data_key: CachingAdapter.CachedDataKey, params: Tuple[Any, ...],
    ):
        models.CacheInfo.delete().where(
            models.CacheInfo.cache_key == data_key,
            models.CacheInfo.params_hash == util.params_hash(*params),
        ).execute()

        if data_key == CachingAdapter.CachedDataKey.PLAYLIST_DETAILS:
            # Invalidate the corresponding cover art.
            playlist = models.Playlist.get_or_none(models.Playlist.id == params[0])
            if not playlist:
                return

            if playlist.cover_art:
                self._invalidate_cover_art(playlist.cover_art)

        elif data_key == CachingAdapter.CachedDataKey.SONG_FILE:
            # Invalidate the corresponding cover art.
            song = models.Song.get_or_none(models.Song.id == params[0])
            if not song:
                return

            if song.cover_art:
                self._invalidate_cover_art(song.cover_art)

    def _do_delete_data(
        self, data_key: CachingAdapter.CachedDataKey, params: Tuple[Any, ...],
    ):
        # Delete it from the cache info.
        models.CacheInfo.delete().where(
            models.CacheInfo.cache_key == data_key,
            models.CacheInfo.params_hash == util.params_hash(*params),
        ).execute()

        def delete_cover_art(cover_art_id: str):
            cover_art_params_hash = util.params_hash(cover_art_id)
            if cover_art_file := self.cover_art_dir.joinpath(cover_art_params_hash):
                cover_art_file.unlink(missing_ok=True)
            self._invalidate_cover_art(cover_art_id)

        if data_key == CachingAdapter.CachedDataKey.PLAYLIST_DETAILS:
            # Delete the playlist and corresponding cover art.
            playlist = models.Playlist.get_or_none(models.Playlist.id == params[0])
            if not playlist:
                return

            if playlist.cover_art:
                delete_cover_art(playlist.cover_art)

            playlist.delete_instance()

        elif data_key == CachingAdapter.CachedDataKey.SONG_FILE:
            song = models.Song.get_or_none(models.Song.id == params[0])
            if not song:
                return

            # Delete the song
            music_filename = self.music_dir.joinpath(song.path)
            music_filename.unlink(missing_ok=True)

            # Delete the corresponding cover art.
            if song.cover_art:
                delete_cover_art(song.cover_art)
