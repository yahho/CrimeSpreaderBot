import datetime
import traceback
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen import oggvorbis
from mutagen import flac
import asyncio
import os
import sys
import traceback
import requests
import zipfile
import re
import time
from collections import deque
from itertools import islice
from random import shuffle

from .utils import get_header
from .entry import URLPlaylistEntry
from .exceptions import ExtractionError, WrongEntryTypeError
from .lib.event_emitter import EventEmitter
from .config import Config, ConfigDefaults


class Playlist(EventEmitter):
    """
        A playlist is manages the list of songs that will be played.
    """

    def __init__(self, bot, config_file=ConfigDefaults.options_file):
        super().__init__()
        self.bot = bot
        self.loop = bot.loop
        #self.thread_pool = ThreadPoolExecutor(max_workers=2)
        self.downloader = bot.downloader
        self.entries = deque()
        self.osz_url = "https://osu.ppy.sh/d/"
        self.config = Config(config_file)
        self.sess = None
        self.osumdir = self.config.osumdir
        self.osulogon = False

    def __iter__(self):
        return iter(self.entries)

    def login(self):
        sess = requests.session()
        params = {
            'username': '%s' % self.config.osuid,
            'password': '%s' % self.config.osupassword,
            'sid': '',
            'login': 'Login'
        }
        res = sess.post('https://osu.ppy.sh/forum/ucp.php?mode=login', data=params)
        print ("[osu!にログイン] サーバーからの応答（ステータスコード）：%s" % res.status_code)
        self.sess = sess

    def shuffle(self):
        shuffle(self.entries)

    def clear(self):
        self.entries.clear()

    async def add_entry(self, song_url, **meta):
        """
            Validates and adds a song_url to be played. This does not start the download of the song.

            Returns the entry & the position it is in the queue.

            :param song_url: The song url to add to the playlist.
            :param meta: Any additional metadata to add to the playlist entry.
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(song_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % song_url)

        # TODO: Sort out what happens next when this happens
        if info.get('_type', None) == 'playlist':
            raise WrongEntryTypeError("This is a playlist.", True, info.get('webpage_url', None) or info.get('url', None))

        if info['extractor'] in ['generic', 'Dropbox']:
            try:
                # unfortunately this is literally broken
                # https://github.com/KeepSafe/aiohttp/issues/758
                # https://github.com/KeepSafe/aiohttp/issues/852
                content_type = await get_header(self.bot.aiosession, info['url'], 'CONTENT-TYPE')
                print("Got content type", content_type)

            except Exception as e:
                print("[Warning] Failed to get content type for url %s (%s)" % (song_url, e))
                content_type = None

            if content_type:
                if content_type.startswith(('application/', 'image/')):
                    if '/ogg' not in content_type:  # How does a server say `application/ogg` what the actual fuck
                        raise ExtractionError("Invalid content type \"%s\" for url %s" % (content_type, song_url))

                elif not content_type.startswith(('audio/', 'video/')):
                    print("[Warning] Questionable content type \"%s\" for url %s" % (content_type, song_url))

        entry = URLPlaylistEntry(
            self,
            song_url,
            info.get('title', 'Untitled'),
            info.get('duration', 0) or 0,
            self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def import_from(self, playlist_url, **meta):
        """
            Imports the songs from `playlist_url` and queues them to be played.

            Returns a list of `entries` that have been enqueued.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """
        position = len(self.entries) + 1
        entry_list = []

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        # Once again, the generic extractor fucks things up.
        if info.get('extractor', None) == 'generic':
            url_field = 'url'
        else:
            url_field = 'webpage_url'

        baditems = 0
        for items in info['entries']:
            if items:
                try:
                    entry = URLPlaylistEntry(
                        self,
                        items[url_field],
                        items.get('title', 'Untitled'),
                        items.get('duration', 0) or 0,
                        self.downloader.ytdl.prepare_filename(items),
                        **meta
                    )

                    self._add_entry(entry)
                    entry_list.append(entry)
                except:
                    baditems += 1
                    # Once I know more about what's happening here I can add a proper message
                    traceback.print_exc()
                    print(items)
                    print("Could not add item")
            else:
                baditems += 1

        if baditems:
            print("Skipped %s bad entries" % baditems)

        return entry_list, position

    async def async_process_youtube_playlist(self, playlist_url, **meta):
        """
            Processes youtube playlists links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0
        for entry_data in info['entries']:
            if entry_data:
                baseurl = info['webpage_url'].split('playlist?list=')[0]
                song_url = baseurl + 'watch?v=%s' % entry_data['id']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)
                except ExtractionError:
                    baditems += 1
                except Exception as e:
                    baditems += 1
                    print("There was an error adding the song {}: {}: {}\n".format(
                        entry_data['id'], e.__class__.__name__, e))
            else:
                baditems += 1

        if baditems:
            print("Skipped %s bad entries" % baditems)

        return gooditems

    async def async_process_sc_bc_playlist(self, playlist_url, **meta):
        """
            Processes soundcloud set and bancdamp album links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0
        for entry_data in info['entries']:
            if entry_data:
                song_url = entry_data['url']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)
                except ExtractionError:
                    baditems += 1
                except Exception as e:
                    baditems += 1
                    print("There was an error adding the song {}: {}: {}\n".format(
                        entry_data['id'], e.__class__.__name__, e))
            else:
                baditems += 1

        if baditems:
            print("Skipped %s bad entries" % baditems)

        return gooditems

    def _add_entry(self, entry):
        self.entries.append(entry)
        self.emit('entry-added', playlist=self, entry=entry)

        if self.peek() is entry:
            entry.get_ready_future()

    async def get_next_entry(self, predownload_next=True):
        """
            A coroutine which will return the next song or None if no songs left to play.

            Additionally, if predownload_next is set to True, it will attempt to download the next
            song to be played - so that it's ready by the time we get to it.
        """
        if not self.entries:
            return None

        entry = self.entries.popleft()

        if predownload_next:
            next_entry = self.peek()
            if next_entry:
                next_entry.get_ready_future()

        return await entry.get_ready_future()

    def peek(self):
        """
            Returns the next entry that should be scheduled to be played.
        """
        if self.entries:
            return self.entries[0]

    async def estimate_time_until(self, position, player):
        """
            (very) Roughly estimates the time till the queue will 'position'
        """
        estimated_time = sum([e.duration for e in islice(self.entries, position - 1)])

        # When the player plays a song, it eats the first playlist item, so we just have to add the time back
        if not player.is_stopped and player.current_entry:
            estimated_time += player.current_entry.duration - player.progress

        return datetime.timedelta(seconds=estimated_time)

    def count_for_user(self, user):
        return sum(1 for e in self.entries if e.meta.get('author', None) == user)

    def osu_apl(self):
        files = os.listdir(self.osumdir)
        files_dir = [f for f in files if os.path.isdir(os.path.join(self.osumdir, f))]
        return files_dir

    async def download(self, osz_id):
        if not self.osulogon:
            self.login()
            self.osulogon = True
        dres = self.sess.get("https://osu.ppy.sh/d/" + osz_id, stream=True)
        if dres.headers['Content-Type'] == 'application/download':
            print(dres.headers)
            raw_url = dres.history[0].headers['Location']
            fname =raw_url[raw_url.find("?fs=")+4:raw_url.find("&fd=")].replace("%20", " ")
            print("ファイル名：{}".format(fname))
            osuapl = []
            osuapl.append(self.osu_apl())
            if fname.split(".")[0] in self.osu_apl():
                print ("[osu!譜面ダウンローダー]もうあるみたいだよ？")
                return os.path.join(self.osumdir, fname.split(".")[0])
            else:
                with open(fname, 'wb') as file:
                    for chunk in dres.iter_content(chunk_size=16384):
                        if chunk:
                            file.write(chunk)
                            file.flush()
                    file.close()
                    return fname
        else:
            self.login()
            dres = self.sess.get("https://osu.ppy.sh/d/" + osz_id, stream=True)
            raw_url = dres.history[0].headers['Location']
            fname =raw_url[raw_url.find("?fs=")+4:raw_url.find("&fd=")].replace("%20", " ")
            print("ファイル名：{}".format(fname))
            osuapl = []
            osuapl.append(self.osu_apl())
            if fname.split(".")[0] in self.osu_apl():
                print ("[osu!譜面ダウンローダー]もうあるみたいだよ？")
                return os.path.join(self.osumdir, fname.split(".")[0])
            else:
                with open(fname, 'wb') as file:
                    for chunk in dres.iter_content(chunk_size=16384):
                        if chunk:
                            file.write(chunk)
                            file.flush()
                    file.close()
                    return fname

    async def unzip(self, osz, dcdir):
        with zipfile.ZipFile(osz, 'r') as zip_file:
            zip_file.extractall(path=dcdir)
        os.remove(osz)

    async def detecter(self, songdir):
        #リストアップ
        files = os.listdir(songdir)
        files_file = [f for f in files if os.path.isfile(os.path.join(songdir, f))]
        #osuファイル検出
        osu_detect = [l for l in files_file if l.endswith('.osu')]
        osu_osup = os.path.join(songdir, osu_detect[0])
        osu_osuo = open(osu_osup, encoding='utf_8')
        osu_raw = osu_osuo.readlines()
        osu_osuo.close()
        AFn = None
        Tit = None
        UTit = None
        dosz_id = None
        
        for line in osu_raw:
            line = line.replace('\n', '')
            if line.startswith("AudioFilename:"):
                AFn = line[15:]
                continue
            elif line.startswith("Title:"):
                Tit = line[6:]
                continue
            elif line.startswith("TitleUnicode:"):
                UTit = line[13:]
                break
            elif line.startswith("[Difficulty]"):
                break
        
        if UTit:
            Tit = UTit
        AudioFname = os.path.join(songdir, AFn)
        if AudioFname.endswith(".mp3") or AudioFname.endswith(".MP3"):
            af = MP3(AudioFname)
        elif AudioFname.endswith(".ogg") or AudioFname.endswith(".OGG"):
            af = oggvorbis.OggVorbis(AudioFname)
        elif AudioFname.endswith(".m4a") or AudioFname.endswith(".M4A"):
            af = MP4(AudioFname)
        elif AudioFname.endswith(".flac") or AudioFname.endswith(".FLAC"):
            af = flac.FLAC(AudioFname)
        duration = int(af.info.length)+1
        dosz_idd = os.path.dirname(songdir)
        dosz_ids = songdir.replace(dosz_idd, '').split(' ')[0]
        dosz_id = dosz_ids.replace('\\', '')
        print("検出ID：{}".format(dosz_id))
        return Tit, AudioFname, duration, dosz_id

    def remove_start(self, s, start):
        return s[len(start):] if s is not None and s.startswith(start) else s

    def sanitize_path(self, s):
        """Sanitizes and normalizes path on Windows"""
        if sys.platform != 'win32':
            return s
        drive_or_unc, _ = os.path.splitdrive(s)
        if sys.version_info < (2, 7) and not drive_or_unc:
            drive_or_unc, _ = os.path.splitunc(s)
        norm_path = os.path.normpath(self.remove_start(s, drive_or_unc)).split(os.path.sep)
        if drive_or_unc:
            norm_path.pop(0)
        sanitized_path = [
            path_part if path_part in ['.', '..'] else re.sub(r'(?:[/<>:"\|\\?\*]|[\s.]$)', '#', path_part)
            for path_part in norm_path]
        if drive_or_unc:
            sanitized_path.insert(0, drive_or_unc + os.path.sep)
        return os.path.join(*sanitized_path)

    async def add_entry_raw(self, osz_id = None, songdir = None, **meta):
        if not osz_id and not songdir:
            raise exceptions.CommandError("レジストにはIDまたはディレクトリの指定が必要です", expire_in=30)
        elif not osz_id:
            print("ローカル実行")
            dsongdir = os.path.join(self.osumdir, songdir)
            title, music_filename, duration, osz_idd = await self.detecter(dsongdir)
        else:
            osz = await self.download(osz_id)
            print(osz)
            if osz.endswith(".osz"):
                omdir = self.config.osumdir
                namedir = osz.split(".")[0]
                print("ディレクトリ：{}\\{}".format(omdir, namedir))
                print("Windowsのディレクトリ（テスト。os.environ.get('windir')の結果が出る）：{}".format(os.environ.get('windir')))
                dcdir = os.path.join(omdir, namedir)
                os.mkdir(dcdir)
                await self.unzip(osz, dcdir)
                title, music_filename, duration, _ = await self.detecter(dcdir)
            else:
                songdir = osz
                title, music_filename, duration, _ = await self.detecter(songdir)
        
        print("サニタイズ（完全文字列化）前：{}".format(music_filename))
        audio_filename = self.sanitize_path(music_filename)
        print("サニタイズ後：{}".format(audio_filename))
        if not osz_id and osz_idd:
            print("検出されたoszのID：{}".format(osz_idd))
            entry = URLPlaylistEntry(
                self,
                "https://osu.ppy.sh/s/" + osz_idd,
                "[osu!譜面]" + title,
                duration,
                expected_filename=audio_filename,
                **meta
            )
            self.entries.append(entry)
            self.emit('entry-added', playlist=self, entry=entry)

            if self.peek() is entry:
                entry.get_ready_future()
            return entry, len(self.entries)
        
        elif osz_id:
            print("指定されたoszのID：{}".format(osz_id))
            entry = URLPlaylistEntry(
                self,
                "https://osu.ppy.sh/s/" + osz_id,
                "[osu!譜面]" + title,
                duration,
                expected_filename=audio_filename,
                **meta
            )
            self.entries.append(entry)
            self.emit('entry-added', playlist=self, entry=entry)

            if self.peek() is entry:
                entry.get_ready_future()

            return entry, len(self.entries)

