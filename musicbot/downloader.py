import os
import asyncio
import functools
import youtube_dl
import zipfile

from concurrent.futures import ThreadPoolExecutor
from .config import Config, ConfigDefaults
from .entry import OsuLocalPlaylistEntry
#from .bot import Response

ytdl_format_options = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'username': 'fumimail_8pc@yahoo.co.jp',
    'password': 'nandat24q'
}

# Fuck your useless bugreports message that gets two link embeds and confuses users
youtube_dl.utils.bug_reports_message = lambda: ''

'''
    Alright, here's the problem.  To catch youtube-dl errors for their useful information, I have to
    catch the exceptions with `ignoreerrors` off.  To not break when ytdl hits a dumb video
    (rental videos, etc), I have to have `ignoreerrors` on.  I can change these whenever, but with async
    that's bad.  So I need multiple ytdl objects.

'''

class Downloader:
    def __init__(self, bot, download_folder=None, config_file=ConfigDefaults.options_file):
        self.bot = bot
        self.thread_pool = ThreadPoolExecutor(max_workers=4)
        self.unsafe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl.params['ignoreerrors'] = True
        self.download_folder = download_folder
        #self.osuDLloop = asyncio.get_event_loop()
        self.config = Config(config_file)

        if download_folder:
            otmpl = self.unsafe_ytdl.params['outtmpl']
            self.unsafe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)
            # print("setting template to " + os.path.join(download_folder, otmpl))

            otmpl = self.safe_ytdl.params['outtmpl']
            self.safe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)


    @property
    def ytdl(self):
        return self.safe_ytdl

    async def extract_info(self, loop, *args, on_error=None, retry_on_error=False, **kwargs):
        """
            Runs ytdl.extract_info within the threadpool. Returns a future that will fire when it's done.
            If `on_error` is passed and an exception is raised, the exception will be caught and passed to
            on_error as an argument.
        """
        if callable(on_error):
            try:
                return await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))

            except Exception as e:

                # (youtube_dl.utils.ExtractorError, youtube_dl.utils.DownloadError)
                # I hope I don't have to deal with ContentTooShortError's
                if asyncio.iscoroutinefunction(on_error):
                    asyncio.ensure_future(on_error(e), loop=loop)

                elif asyncio.iscoroutine(on_error):
                    asyncio.ensure_future(on_error, loop=loop)

                else:
                    loop.call_soon_threadsafe(on_error, e)

                if retry_on_error:
                    return await self.safe_extract_info(loop, *args, **kwargs)
        else:
            return await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))

    async def safe_extract_info(self, loop, *args, **kwargs):
        return await loop.run_in_executor(self.thread_pool, functools.partial(self.safe_ytdl.extract_info, *args, **kwargs))

    def osuDL(self, playlist, osz_id, fname, dres, busymsg, player, bidhash, **meta):
        with open(fname, 'wb') as file:
            for chunk in dres.iter_content(chunk_size=64 * 1024):
                if chunk:
                    file.write(chunk)
                    #file.flush()
            file.close()
        print("ダウンロード完了!")
        dcdir = os.path.join(self.config.osumdir, os.path.splitext(fname)[0])
        os.mkdir(dcdir)
        with zipfile.ZipFile(fname, 'r') as zip_file:
            zip_file.extractall(path=dcdir)
        os.remove(fname)
        print (meta.items())
        #ch = meta['channel']
        #at = meta['author']
        title, music_filename, duration, _ = playlist.detecter(dcdir, bidhash=bidhash)
        audio_filename = playlist.sanitize_path(music_filename)
        entrypack = []
        if bidhash:
            entry = OsuLocalPlaylistEntry(
                playlist,
                "https://osu.ppy.sh/s/" + osz_id,
                "https://osu.ppy.sh/beatmapsets/{}#{}/{}".format(osz_id, bidhash[2], bidhash[0]),
                "[osu!譜面]" + title,
                duration,
                filename=audio_filename,
                **meta
            )
            playlist.entries.append(entry)
            playlist.emit('entry-added', playlist=playlist, entry=entry)

            if playlist.peek() is entry:
                entry.get_ready_future()
            entrypack.append((entry, len(playlist.entries)))
        else:
            entry = OsuLocalPlaylistEntry(
                playlist,
                "https://osu.ppy.sh/s/" + osz_id,
                "https://osu.ppy.sh/beatmapsets/{}".format(osz_id),
                "[osu!譜面]" + title,
                duration,
                filename=audio_filename,
                **meta
            )
            playlist.entries.append(entry)
            playlist.emit('entry-added', playlist=playlist, entry=entry)

            if playlist.peek() is entry:
                entry.get_ready_future()
            entrypack.append((entry, len(playlist.entries)))
        #reply_text = " osu!譜面：**{}**をプレイリストに追加しました。~~多分~~"
        #btext = entrypack[0][0].title

        #reply = reply_text.format(btext)
        #self.bot.safe_send_message(ch, reply, expire_in=15, also_delete=busymsg)
        #self.bot.fin_add_entry(player, osz_id, busymsg, ch, at)
        return entrypack[0]

    async def osuDown(self, playlist, osz_id, fname, dres, busymsg=None, player=None, bidhash=None, **meta):
        #try:
        
        #await self.bot.fin_add_entry(id, msg, ch, at)
        #self.osuDLloop.run_forever()
        return await self.bot.loop.run_in_executor(self.thread_pool, functools.partial(self.osuDL, playlist, osz_id, fname, dres, busymsg=busymsg, player=player, bidhash=bidhash, **meta))

        #except Exception as e:

            # (youtube_dl.utils.ExtractorError, youtube_dl.utils.DownloadError)
            # I hope I don't have to deal with ContentTooShortError's
            #if asyncio.iscoroutinefunction(on_error):
                #asyncio.ensure_future(on_error(e), loop=loop)

            #elif asyncio.iscoroutine(on_error):
                #asyncio.ensure_future(on_error, loop=loop)

            #else:
                #loop.call_soon_threadsafe(on_error, e)
