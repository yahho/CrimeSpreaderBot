import asyncio
import os
import traceback
import requests
import zipfile
import re
import time

from .exceptions import ExtractionError, WrongEntryTypeError
from .utils import get_header, md5sum
from .entry import URLPlaylistEntry
from .lib.event_emitter import EventEmitter
from musicbot.config import Config, ConfigDefaults



class OsuPlay:
    def __init__(self, bot, Playlist):
        super().__init__()
        #self.fname = None
        #self._is_downloading = False
        #self._waiting_futures = []
        #self.bot = bot
        #self.loop = bot.loop
        #self.downloader = bot.downloader
        #self.entries = Playlist.entries
        self.osumdir = os.environ.get('osudir')
        self.osz_url = "https://osu.ppy.sh/d/"


    def login():
        Atime = time.time()
        sess = requests.session()
        params = {
            'username': '%s' % osuid,
            'password': '%s' % osupassword,
            'sid': '',
            'login': 'Login'
        }
        res = sess.post('https://osu.ppy.sh/forum/ucp.php?mode=login', data=params)
        print ("[osu!にログイン] サーバーからの応答（ステータスコード）：%s" % res.status_code)
        return sess
    

#    async def download(self, osz_id):
#        dres = sess.get("https://osu.ppy.sh/d/" + osz_id, stream=True)
#        if dres.status_code == 200:
#            raw_url = dres.history[0].headers['Location']
#            fname =raw_url[raw_url.find("?fs=")+4:raw_url.find("&fd=")-1].replace("%20", " ")
#            if self.osu_apl().count(fname.split(".")[0]):
#                print ("[osu!譜面ダウンローダー]もうあるみたいだよ？")
#                return osumdir + fname.split(".")[0]
#            else:
#                with open(fname, 'wb') as file:
#                    for chunk in dres.iter_content(chunk_size=16384):
#                        if chunk:
#                            file.write(chunk)
#                            file.flush()
#                    return fname
    

    #playlist
#    async def add_entry_raw(self, osz_id = None, songdir = None, **meta):
#        if not osz_id not songdir:
#            raise exceptions.CommandError("レジストにはIDまたはディレクトリの指定が必要です", expire_in=30)
#        elif not osz_id:
#            title, music_filename, duration, osz_id = self.detecter(songdir)
#        else:
#            osz = await self.download(osz_id)
#            if osz.endswith(".osz"):
#                dcdir = os.mkdir(osumdir + osz.split(".")[0])
#                await self.unzip(osz, dcdir)
#                title, music_filename, duration, _ = self.detecter(dcdir)
#            else:
#                title, music_filename, duration, _ = self.detecter(songdir)
        
#        entry = URLPlaylistEntry(
#            self,
#            "https://osu.ppy.sh/s/" + osz_id,
#            "[osu!譜面]" + title,
#            duration,
#            music_filename,
#            **meta
#        )
#        self.entries.append(entry)
#        self.emit('entry-added', playlist=self, entry=entry)

#        if self.peek() is entry:
#            entry.get_ready_future()

#        return entry, len(self.entries)

#    async def unzip(self, osz, dcdir):
#        raise exceptions.CommandError("未対応です", expire_in=30)
#        with zipfile.ZipFile(osz, 'r') as zip_file:
#            zip_file.extractall(path=dcdir)
    

#    async def detecter(self, songdir):
#        files = os.listdir(songdir)
#        files_file = [f for f in files if os.path.isfile(os.path.join(path, f))]
#        osu_osui = files_file.index(.osu$)
#        osu_osup = songdir + files_file[osu_osui]
#        osu_osuo = open(osu_osup)
#        osu_raw = osu_osuo.readlines()
#        osu_osuo.close()
#        AFn = None
#        Tit = None
#        UTit = None
#        dosz_id = None
        
#        for line in osu_raw:
#            if line.startswith("AudioFilename:"):
#                AFn = line[15:]
#                continue
#            elif line.startswith("Title:"):
#                Tit = line[6:]
#                continue
#            elif line.startswith("TitleUnicode:"):
#                UTit = line[13:]
#                break
#            elif line.startswith("BeatmapSetID:"):
#                dosz_id = line[13:]
#            elif line.startswith("[Difficulty]"):
#                break
        
#        if UTit:
#            Tit = Utit
#        AudioFname = osu_osup + AFn
#        mf = mad.MadFile(AudioFname)
#        duration = mf.total_time()
#        return Tit, AudioFname, duration, dosz_id
    

#    async def osu_apl():
#        files = os.listdir(self.osumdir)
#        files_dir = [f for f in files if os.path.isdir(os.path.join(path, f))]
#        return files_dir
