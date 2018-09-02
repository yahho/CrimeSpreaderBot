import os
import sys
import time
import shlex
import shutil
import inspect
import aiohttp
import discord
import asyncio
import traceback
import urllib
import re
import requests

from discord import utils
from discord.object import Object
from discord.enums import ChannelType
from discord.voice_client import VoiceClient
from discord.ext.commands.bot import _get_variable

from io import BytesIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from random import choice, shuffle
from collections import defaultdict
from osuapi import OsuApi, ReqConnector

from musicbot.playlist import Playlist
from musicbot.player import MusicPlayer
from musicbot.config import Config, ConfigDefaults
from musicbot.permissions import Permissions, PermissionsDefaults
from musicbot.utils import load_file, write_file, sane_round_int

from . import exceptions
from . import downloader
from .opus_loader import load_opus_lib
from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH


load_opus_lib()


class SkipState:
    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    def __init__(self, content, reply=False, delete_after=0):
        self.content = content
        self.reply = reply
        self.delete_after = delete_after


class MusicBot(discord.Client):
    def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file):
        self.players = {}
        self.the_voice_clients = {}
        self.locks = defaultdict(asyncio.Lock)
        self.voice_client_connect_lock = asyncio.Lock()
        self.voice_client_move_lock = asyncio.Lock()

        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)
        self.downloader = downloader.Downloader(self, download_folder='audio_cache')

        self.exit_signal = None
        self.init_ok = False
        self.cached_client_id = None

        self.osumode = False
        self.osuplaylist = None
        self.osumdir = None
        self.osulogon = False
        self.osuapi = OsuApi(self.config.osukey, connector=ReqConnector())
        self.busymsg = None

        if not self.autoplaylist:
            print("Warning: Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False

        # TODO: Do these properly
        ssd_defaults = {'last_np_msg': None, 'auto_paused': False}
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("設置者専用コマンドです", expire_in=30)

        return wrapper

    @staticmethod
    def _fixg(x, dp=2):
        return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

    def _get_owner(self, voice=False):
        if voice:
            for server in self.servers:
                for channel in server.channels:
                    for m in channel.voice_members:
                        if m.id == self.config.owner_id:
                            return m
        else:
            return discord.utils.find(lambda m: m.id == self.config.owner_id, self.get_all_members())

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    # TODO: autosummon option to a specific channel
    async def _auto_summon(self):
        owner = self._get_owner(voice=True)
        if owner:
            self.safe_print("オーナーを \"%s\"で発見, 参加しています..." % owner.voice_channel.name)
            # TODO: Effort
            await self.cmd_モ召喚(owner.voice_channel, owner, None)
            return owner.voice_channel

    async def _autojoin_channels(self, channels):
        joined_servers = []

        for channel in channels:
            if channel.server in joined_servers:
                print("私（たち）は %sにいます:musical_note:  というわけで無視" % channel.server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                self.safe_print("%s の %s に参加しています・・・" % (channel.server.name, channel.name))

                chperms = channel.permissions_for(channel.server.me)

                if not chperms.connect:
                    self.safe_print(" \"%s\"から弾かれたなり:kanashimi:  許可して下さいお願いしますなんかするわけではないけど" % channel.name)
                    continue

                elif not chperms.speak:
                    self.safe_print(" \"%s\"で黙ることを、**強いられているんだッ！！** " % channel.name)
                    continue

                try:
                    player = await self.get_player(channel, create=True)

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        await self.on_player_finished_playing(player)

                    joined_servers.append(channel.server)
                except Exception as e:
                    if self.config.debug_mode:
                        traceback.print_exc()
                    print("参加に失敗しました。", channel.name)

            elif channel:
                print("Not joining %s on %s, that's a text channel." % (channel.name, channel.server.name))

            else:
                print("Invalid channel thing: " + channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def generate_invite_link(self, *, permissions=None, server=None):
        if not self.cached_client_id:
            appinfo = await self.application_info()
            self.cached_client_id = appinfo.id

        return discord.utils.oauth_url(self.cached_client_id, permissions=permissions, server=server)

    def osu_apl(self):
        files = os.listdir(self.config.osumdir)
        numsl = ("1", "2", "3", "4", "5", "6", "7", "8", "9")
        files_dir = [f for f in files if os.path.isdir(os.path.join(self.config.osumdir, f))  and f.startswith(numsl)]
        return files_dir

    async def get_voice_client(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.the_voice_clients:
                return self.the_voice_clients[server.id]

            s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
            _voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

            await self.ws.voice_state(server.id, channel.id)

            s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
            voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
            session_id = s_id_data.get('session_id')

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }
            voice_client = VoiceClient(**kwargs)
            self.the_voice_clients[server.id] = voice_client

            retries = 3
            for x in range(retries):
                try:
                    print("接続を確立しています・・・")
                    await asyncio.wait_for(voice_client.connect(), timeout=10, loop=self.loop)
                    print("接続を確立しました。.")
                    break
                except:
                    traceback.print_exc()
                    print("接続に失敗しました。再試行中。。。 (%s/%s)..." % (x+1, retries))
                    await asyncio.sleep(1)
                    await self.ws.voice_state(server.id, None, self_mute=True)
                    await asyncio.sleep(1)

                    if x == retries-1:
                        raise exceptions.HelpfulError(
                            "ボイスチャットへの接続を確立できません  "
                            "何かUDPの上り接続をブロック、または重複しているものがないか確認して下さい。",

                            "この問題はファイアウォールがUDP通信をブロックしていることによって発生することがあります。  "
                            "利用しているファイアウォールのUDP通信の制限に関係する設定の確認と変更をお願いします。"
                        )

            
            return voice_client

    async def mute_voice_client(self, channel, mute):
        await self._update_voice_state(channel, mute=mute)

    async def deafen_voice_client(self, channel, deaf):
        await self._update_voice_state(channel, deaf=deaf)

    async def move_voice_client(self, channel):
        await self._update_voice_state(channel)

    async def reconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        vc = self.the_voice_clients.pop(server.id)
        _paused = False

        player = None
        if server.id in self.players:
            player = self.players[server.id]
            if player.is_playing:
                player.pause()
                _paused = True

        try:
            await vc.disconnect()
        except:
            print("Error disconnecting during reconnect")
            traceback.print_exc()

        await asyncio.sleep(0.1)

        if player:
            new_vc = await self.get_voice_client(vc.channel)
            player.reload_voice(new_vc)

            if player.is_paused and _paused:
                player.resume()

    async def disconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await self.the_voice_clients.pop(server.id).disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in self.the_voice_clients.copy().values():
            await self.disconnect_voice_client(vc.channel.server)

    async def _update_voice_state(self, channel, *, mute=False, deaf=False):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        # I'm not sure if this lock is actually needed
        with await self.voice_client_move_lock:
            server = channel.server

            payload = {
                'op': 4,
                'd': {
                    'guild_id': server.id,
                    'channel_id': channel.id,
                    'self_mute': mute,
                    'self_deaf': deaf
                }
            }

            await self.ws.send(utils.to_json(payload))
            self.the_voice_clients[server.id].channel = channel

    async def get_player(self, channel, create=False) -> MusicPlayer:
        server = channel.server

        if server.id not in self.players:
            if not create:
                raise exceptions.CommandError(
                    '絶っ対VCに参加してやるぜぇぇ！！\n   待ってろ:nubesco:ライフゥゥェァッッッ！！！  '
                    '**もしかして** %ssummon を忘れてませんか？' % self.config.command_prefix)

            voice_client = await self.get_voice_client(channel)

            playlist = Playlist(self)
            player = MusicPlayer(self, voice_client, playlist) \
                .on('play', self.on_player_play) \
                .on('resume', self.on_player_resume) \
                .on('pause', self.on_player_pause) \
                .on('stop', self.on_player_stop) \
                .on('finished-playing', self.on_player_finished_playing) \
                .on('entry-added', self.on_player_entry_added)

            player.skip_state = SkipState()
            self.players[server.id] = player

        return self.players[server.id]

    async def on_player_play(self, player, entry):
        await self.update_now_playing(entry)
        player.skip_state.reset()

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = 'Hey %s. 追加した曲の **%s** が %sで絶賛垂れ流し中ですよ!:nubesco:' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = '%sで再生中:projector: : **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

    async def on_player_resume(self, entry, **_):
        await self.update_now_playing(entry)

    async def on_player_pause(self, entry, **_):
        await self.update_now_playing(entry, True)

    async def on_player_stop(self, **_):
        await self.update_now_playing()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist and not self.osumode:
            while self.autoplaylist:
                song_url = choice(self.autoplaylist)
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)

                if not info:
                    self.autoplaylist.remove(song_url)
                    self.safe_print("[Info] Removing unplayable song from autoplaylist: %s" % song_url)
                    write_file(self.config.auto_playlist_file, self.autoplaylist)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    pass  # Wooo playlist
                    # Blarg how do I want to do this

                # TODO: better checks here
                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    print("Error adding song from autoplaylist:", e)
                    continue

                break

            if not self.autoplaylist:
                print("[警告] 再生不可能なAPLです。設定は無効化されました。")
                self.config.auto_playlist = False
        elif not player.playlist.entries and not player.current_entry and self.config.auto_playlist and self.osumode:
#            osu_apli = []
#            osu_apli.append(self.osu_apl())
            while self.osu_apl():
                songdir = choice(self.osu_apl())
                print("選出されたフォルダ: %s" % songdir)
                await player.playlist.add_entry_raw(osz_id=None, songdir=songdir)
                
                break

            if not self.osu_apl():
                print("[警告] 再生不可能なAPLです。設定は無効化されました。osu!のSongsディレクトリを適切に設定したか確認して下さい。")
                self.config.auto_playlist = False

    async def on_player_entry_added(self, playlist, entry, **_):
        pass

    async def update_now_playing(self, entry=None, is_paused=False):
        game = discord.Game(name="ちょっとまってね・・・")

        if self.user.bot:
            activeplayers = sum(1 for p in self.players.values() if p.is_playing)
            if activeplayers > 1:
                game = discord.Game(name="現在、%s々所のサーバーで栗目" % activeplayers)
                entry = None

            elif activeplayers == 1:
                player = discord.utils.get(self.players.values(), is_playing=True)
                entry = player.current_entry

        if entry:
            prefix = u'\u275A\u275A ' if is_paused else ''

            name = u'{}{}'.format(prefix, entry.title)[:128]
            game = discord.Game(name=name)

        await self.change_presence(game=game)


    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, no permission" % dest.name)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, invalid channel?" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", no permission" % message.clean_content)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", message not found" % message.clean_content)

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot edit message \"%s\", message not found" % message.clean_content)
            if send_if_fail:
                if not quiet:
                    print("Sending instead")
                return await self.safe_send_message(message.channel, new)

    def safe_print(self, content, *, end='\n', flush=True):
        sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
        if flush: sys.stdout.flush()

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            if self.config.debug_mode:
                print("Could not send typing to %s, no permssion" % destination)

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: # Can be ignored
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: # Can be ignored
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Email or Password or Token in the options file.  "
                "Remember that each field should be on their own line.")

        finally:
            try:
                self._cleanup()
            except Exception as e:
                print("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            print("Exception in", event)
            print(ex.message)

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            traceback.print_exc()

    async def on_resumed(self):
        for vc in self.the_voice_clients.values():
            vc.main_ws = self.ws

    async def on_ready(self):
        print('\rログイン完了！  Musicbot v%s\n' % BOTVERSION)

        if self.config.owner_id == self.user.id:
            raise exceptions.HelpfulError(
                "不正なオーナーIDが設定されています。  ",

                "ボットは搭載された機能のためにこの情報を必要とします。  "
                "オーナIDとはボット所有者のIDであり、ボットのIDではありません。  "
                "混同しないよう正しい情報を設定して下さい。")

        self.init_ok = True

        self.safe_print("ボットの情報:   %s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            self.safe_print("オーナーの情報: %s/%s#%s\n" % (owner.id, owner.name, owner.discriminator))

            print('アクセス可能なサーバー:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        elif self.servers:
            print("オーナーをアクセス可能なサーバーから発見できませんでした (id: %s)\n" % self.config.owner_id)

            print('アクセス可能なサーバー:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        else:
            print("オーナーが不明です。ボットはどのサーバーでもアクセスできません。")
            if self.user.bot:
                print("\n以下のリンクにブラウザでアクセスし、ボットの接続を承認して下さい。")
                print("注意: 追加したいサーバーのサーバー管理権限を所有するユーザーで \n"
                      "ログインしている必要があります。\n")
                print("    リンク：" + await self.generate_invite_link())

        print()

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            print("ボットに固定されたテキストチャンネル:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nNot binding to voice channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print()

        else:
            print("ボットに固定されたテキストチャンネルはありません")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            print("自動接続設定されたボイスチャンネル:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nCannot join text channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            print("自動接続設定されたボイスチャンネルはありません。")
            autojoin_channels = set()

        print()
        print("設定情報:")

        self.safe_print("  メインコマンドプレフィックス: " + self.config.command_prefix)
        self.safe_print("  サブコマンドプレフィックス: " + self.config.subcommand_prefix)
        print("  標準の音量: %s%%" % int(self.config.default_volume * 100))
        print("  スキップの条件: %s 票または %s%%の投票" % (
            self.config.skips_required, self._fixg(self.config.skip_ratio_required * 100)))
        print("  再生開始メンション: " + ['無効', '有効'][self.config.now_playing_mentions])
        print("  自動参加: " + ['無効', '有効'][self.config.auto_summon])
        print("  無リクエスト時に自動プレイリスト再生: " + ['無効', '有効'][self.config.auto_playlist])
        print("  無人時に一時停止: " + ['無効', '有効'][self.config.auto_pause])
        print("  ボットメッセージ削除: " + ['無効', '有効'][self.config.delete_messages])
        if self.config.delete_messages:
            print("    コマンド利用者メッセージの削除: " + ['無効', '有効'][self.config.delete_invoking])
        print("  デバッグモード: " + ['無効', '有効'][self.config.debug_mode])
        print("  ダウンロードキャッシュを %s" % ['削除する', '保存する'][self.config.save_videos])
        print()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                print("古いキャッシュを削除しています・・・")
            else:
                print("古いキャッシュを削除できませんでした。")

        if self.config.autojoin_channels:
            await self._autojoin_channels(autojoin_channels)

        elif self.config.auto_summon:
            print("オーナーが参加中のボイスチャットに自動参加しています・・・", flush=True)

            # waitfor + get value
            owner_vc = await self._auto_summon()

            if owner_vc:
                print("成功しました！", flush=True)  # TODO: Change this to "Joined server/channel"
                if self.config.auto_playlist:
                    print("自動プレイリスト再生を開始します。。。")
                    await self.on_player_finished_playing(await self.get_player(owner_vc))
            else:
                print("オーナーが参加中のボイスチャンネルはありませんでした、自動参加に失敗しました。")

        print()
        # t-t-th-th-that's all folks!

    async def cmd_help(self, command=None):
        """
        Usage:
            {command_prefix}help [command]

        ヘルプを表示するよ！
        コマンド名称が指定された場合は該当するコマンドの説明が返されます。
        それ以外では、ボットが持つコマンドをリストアップします。
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd:
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__),
                        command_prefix=self.config.command_prefix
                    ),
                    delete_after=60
                )
            else:
                return Response("そんなコマンドあったっけ<:gaito88:257807307533058049>", delete_after=10)

        else:
            helpmsg = "**コマンド一覧**\n"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help':
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}か{}で`{}`".format(self.config.command_prefix, self.config.subcommand_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "\nこのBotは以下のサイトで公開されているBotを改造したものです\n"
            helpmsg += "https://github.com/SexualRhinoceros/MusicBot/wiki/Commands-list"
            helpmsg += "\nコマンドの使い方がわからないときは{}または{}で`help [調べたいコマンド]`で調べられるぞ！<:kame:264244926311563265>".format(self.config.command_prefix, self.config.subcommand_prefix)

            return Response(helpmsg, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        Usage:
            {プレフィックス}blacklist [ + | - | add | remove ] @ユーザー1 [@ユーザー2 ...]

        指定したユーザーをブラックリストに追加、または除去を行います。
        ボットはブラックリストに追加されたユーザーからのコマンドを拒絶します。
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {プレフィックス}id [@ユーザー]

        コマンド発行者や指定されたユーザーのIDを返します。
        """
        if not user_mentions:
            return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        Usage:
            {command_prefix}joinserver invite_link

        Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
        """

        if self.user.bot:
            url = await self.generate_invite_link()
            return Response(
                "Bot accounts can't use invite links!  Click here to invite me: \n{}".format(url),
                reply=True, delete_after=30
            )

        try:
            if server_link:
                await self.accept_invite(server_link)
                return Response(":+1:")

        except:
            raise exceptions.CommandError('Invalid URL provided:\n{}\n'.format(server_link), expire_in=30)

    async def cmd_オート変更(self, message, channel, author, leftover_args):
        """
        使い方：
            {プレフィックス}オート変更 ファイルの相対位置（config下から）
            {プレフィックス}オート変更 URL
            {プレフィックス}オート変更（テキストファイル[.txt]添付）
            
            テキストファイル[.txt]のURLで変更ができるようになりました（多分）
                注：テキストファイルの直リンク、またはそれに自動的にリダイレクトされるURLにして下さい
            テキストファイル[.txt]の添付で変更ができます
        """
        
        def apldl(apl_url):
            file_name = apl_url.split("/")[-1]
            res = requests.get(apl_url, stream=True)
            if res.status_code == 200:
                with open(file_name, 'wb') as file:
                    for chunk in res.iter_content(chunk_size=1024):
                        if chunk:
                            file.write(chunk)
                            file.flush()
                    return file_name
        
        await self.send_typing(channel)
        self.apl_file = None
        self.apl_file = " ".join(leftover_args)
        self.apl_file = self.apl_file.strip(' ')

        if message.attachments:
            apl_url = message.attachments[0]['url']
            if  apl_url.split("/")[-1].split(".")[-1] != "txt":
                raise exceptions.CommandError("この添付ファイル（リンク：{})は未対応のファイルです".format(apl_url))

            self.config.auto_playlist_file = apldl(apl_url)
            self.autoplaylist = load_file(self.config.auto_playlist_file)
            return Response("オートプレイリストが{1.name}によって`{0}`に変更されました。".format(apl_url, author), delete_after=60)
        elif self.apl_file.startswith("http://") or \
             self.apl_file.startswith("https://"):
            if  self.apl_file.split("/")[-1].split(".")[-1] != "txt":
                raise exceptions.CommandError("このURL({})は現在未対応です".format(self.apl_file))
            self.config.auto_playlist_file = apldl(self.apl_file)
            self.autoplaylist = load_file(self.config.auto_playlist_file)
            return Response("オートプレイリストが{1.name}によって`{0}`に変更されました。".format(apl_url, author), delete_after=60)
        elif not len(self.apl_file):
            raise exceptions.CommandError("引数を指定して下さい")
        else:
            if  self.apl_file.split(".")[-1] != "txt":
                raise exceptions.CommandError("このファイル({})は未対応のファイルです".format(self.apl_file))
            self.config.auto_playlist_file = 'config/{}'.format(self.apl_file)
            self.autoplaylist = load_file(self.config.auto_playlist_file)
            return Response("オートプレイリストが{1.name}によって{0}に変更されました。".format(self.config.auto_playlist_file, author), delete_after=60)

    async def cmd_changeauto(self, message, channel, author, leftover_args):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_オート変更(message=message, channel=channel, author=author, leftover_args=leftover_args)


    async def cmd_osuリログ(self, message, channel, author):
        """
        使い方:
            <プレフィックス>osuリログ
            
            注：乱用しないで下さい
            　　引数は必要ありません。
            　　認証情報はボットのコンフィグに記載のものが使用されます。
        """

        player.playlist.login()
        return Response("ログイン処理を実行しました。:innocent:", delete_after=30)

    async def cmd_osuモード(self, message, channel, author, leftover_args):
        """
        使い方:
            <プレフィックス>osuモード [オン,オフ]
            
            
            引数がなければ現在の設定を返します
            指定された引数があれば有効化、無効化を行います
        """
        
        if self.osumode:
            res_mode = "有効"
        else:
            res_mode = "無効"

        if not leftover_args:
            return Response("現在のosu!APL機能は**{}**です。".format(res_mode), delete_after=30)
        else:
            self.osum = " ".join(leftover_args)
            self.osum = self.osum.strip(' ')
            if self.osum == "オン" or self.osum == "on":
                self.osumode = True
                res_mode = "有効"
                return Response("osu!APL機能は{}に変更されました。".format(res_mode), delete_after=30)
            elif self.osum == "オフ" or self.osum == "off":
                self.osumode = False
                if self.osumode:
                    res_mode = "有効"
                else:
                    res_mode = "無効"
                return Response("osu!APL機能は{}に変更されました。".format(res_mode), delete_after=30)
            else:
                return Response("不正な引数です。", delete_after=30)

    async def cmd_ステータス(self, player, message, channel, author, leftover_args):
        embed = discord.Embed(title="ステータス", description="現在の栗目拡散器の状態はこの通りです。\nこの表示は開発中です。", color=0xdec049)
        embed.set_author(name="yahho's 栗目拡散器", url='https://github.com/yahho/CrimeSpreaderBot', icon_url='https://cdn.discordapp.com/emojis/332181988633083925.png')
        embed.add_field(name="ギルド別設定", value="開発中", inline=False)
        embed.add_field(name="osu!譜面自動再生プレイリスト", value=["❌無効", "✅有効"][self.osumode], inline=True)
        embed.add_field(name="自動再生プレイリスト", value=["❌無効", "✅有効"][self.config.auto_playlist], inline=True)
        embed.add_field(name="音量", value=str(player.volume*100)+"%", inline=True)
        embed.add_field(name="現在再生中の項目", value=[player.current_entry.title+"\n詳細はnpで", "何も再生していません。バグジョンの可能性もあります。"][len(player.current_entry.title)==0], inline=False)
        embed.set_footer(text="再生が止ったときは再起させてみよう")
        return await self.send_message(message.channel, embed=embed)

    async def cmd_osurelogin(self, message, channel, author):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_osuリログ(message=message, channel=channel, author=author)

    async def cmd_osumode(self, message, channel, author, leftover_args):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_osuモード(message=message, channel=channel, author=author, leftover_args=leftover_args)

    #async def ext_futu(futu):
    #    futu.result()

    def fin_add_entry(self, player, osz_id, busymsg, channel, author):
        #if futu.exception():
        #    await self.safe_send_message(channel, "内部エラーが発生しました。該当する栗目は再生されません。")
        #    return
        #else:
        entry, position = player.playlist.add_entry_osu(osz_id=osz_id, channel=channel, author=author)
        reply_text = " osu!譜面セット：**{}**をプレイリストに追加しました。  このosu!譜面セットは: {}"
        btext = entry.title

        if position == 1 and player.is_stopped:
            position = ':white_check_mark:すぐに再生されるよ！'
            reply = reply_text.format(btext, position)

        else:
            try:
                time_until = player.playlist.estimate_time_until_notasync(position, player)
                reply_text += 'あと{}:alarm_clock:栗目後に再生が始まるゾ<:passive:347538399600836608>'
            except:
                traceback.print_exc()
                time_until = ''

            reply = reply_text.format(btext, position, time_until)
        return Response(reply, delete_after=30)
        #return print('展開完了しました')

    async def cmd_登録(self, player, channel, author, permissions, leftover_args, song_url):
        """
        使い方:
            {command_prefix}登録 URL
            {command_prefix}登録 検索ワード
        """
        
        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "栗目大杉なので弾かれましたよ。上限: (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)

        if song_url.startswith('https://osu.ppy.sh/'):
            if song_url.startswith('https://osu.ppy.sh/b/') :
                bid = song_url[21:len(song_url)]
                binfo = self.osuapi.get_beatmaps(beatmap_id=bid)
                bmhash = binfo[0].file_md5
                bidhash = [bid, bmhash]
                osz_idi = binfo[0].beatmapset_id
                osz_id = str(osz_idi)
                #raise exceptions.CommandError("譜面単体のリンクは未対応です。譜面セットのリンクを指定して下さい。", expire_in=30)
#            if not self.osulogon:
#                player.playlist.login()
#                self.osulogon = True
            elif song_url.startswith('https://osu.ppy.sh/beatmapsets/'):
                idEnd = song_url.find('#')
                if idEnd == -1:
                    osz_id = song_url[31:len(song_url)]
                    bidhash = None
                else:
                    osz_id = song_url[31:idEnd-1]
                    bid = song_url.split('/')[-1]
                    binfo = self.osuapi.get_beatmaps(beatmap_id=bid)
                    bmhash = binfo[0].file_md5
                    bidhash = [bid, bmhash]
            
            elif song_url.startswith('https://osu.ppy.sh/s/') or song_url.startswith('https://osu.ppy.sh/d/'):
                osz_id = song_url[21:len(song_url)]
                bidhash = None
            busymsg = await self.safe_send_message(channel, "[**試験機能**]osu!譜面セットのリンク：**{}** の処理を開始しました:arrows_counterclockwise:\nこの処理は開発、修正中のためBotの接続が一時的に切断されるかもしれません。".format(song_url), expire_in=30)
            
#            try:
            await player.playlist.add_entry_raw(osz_id=osz_id, bidhash=bidhash, channel=channel, author=author, busymsg=busymsg, player=None)
            return
#            except:
#                raise exceptions.CommandError("なんか栗目したかも", expire_in=30)
            #await self.safe_delete_message(busymsg)
            #reply_text = " osu!譜面セット：**{}**をプレイリストに追加しました。  このosu!譜面セットは: {}"
            #btext = entry.title

            #if position == 1 and player.is_stopped:
            #    position = ':white_check_mark:すぐに再生されるよ！'
            #    reply = reply_text.format(btext, position)

            #else:
            #    try:
            #        time_until = await player.playlist.estimate_time_until(position, player)
            #        reply_text += 'あと{}:alarm_clock:栗目後に再生が始まるゾ<:passive:347538399600836608>'
            #    except:
            #        traceback.print_exc()
            #        time_until = ''

            #    reply = reply_text.format(btext, position, time_until)
            #return Response(reply, delete_after=30)
        else:
            if leftover_args:
                song_url = ' '.join([song_url, *leftover_args])

            try:
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            except Exception as e:
                raise exceptions.CommandError(e, expire_in=30)

            if not info:
                raise exceptions.CommandError("それは栗目過ぎて再生できません", expire_in=30)

            # abstract the search handling away from the user
            # our ytdl options allow us to use search strings as input urls
            if info.get('url', '').startswith('ytsearch'):
                # print("[Command:play] Searching for \"%s\"" % song_url)
                info = await self.downloader.extract_info(
                    player.playlist.loop,
                    song_url,
                    download=False,
                    process=True,    # ASYNC LAMBDAS WHEN
                    on_error=lambda e: asyncio.ensure_future(
                        self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                    retry_on_error=True
                )

                if not info:
                    raise exceptions.CommandError(
                        "Error extracting info from search string, youtubedl returned no data.  "
                        "You may need to restart the bot if this continues to happen.", expire_in=30
                    )

                if not all(info.get('entries', [])):
                    # empty list, no data
                    return

                song_url = info['entries'][0]['webpage_url']
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
                # But this is probably fine

            # TODO: Possibly add another check here to see about things like the bandcamp issue
            # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

            if 'entries' in info:
                # I have to do exe extra checks anyways because you can request an arbitrary number of search results
                if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                    raise exceptions.PermissionsError("You are not allowed to request playlists", expire_in=30)

                # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
                num_songs = sum(1 for _ in info['entries'])

                if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                    raise exceptions.PermissionsError(
                        "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                        expire_in=30
                    )

                # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
                if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                    raise exceptions.PermissionsError(
                        "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                            num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                        expire_in=30
                    )

                if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                    try:
                        return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                    except exceptions.CommandError:
                        raise
                    except Exception as e:
                        traceback.print_exc()
                        raise exceptions.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

                t0 = time.time()

                # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                # I don't think we can hook into it anyways, so this will have to do.
                # It would probably be a thread to check a few playlists and get the speed from that
                # Different playlists might download at different speeds though
                wait_per_song = 1.2

                procmesg = await self.safe_send_message(
                    channel,
                    'Gathering playlist information for {} songs{}'.format(
                        num_songs,
                        ', ETA: {} seconds'.format(self._fixg(
                            num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                # We don't have a pretty way of doing this yet.  We need either a loop
                # that sends these every 10 seconds or a nice context manager.
                await self.send_typing(channel)

                # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
                #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

                entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

                tnow = time.time()
                ttime = tnow - t0
                listlen = len(entry_list)
                drop_count = 0

                if permissions.max_song_length:
                    for e in entry_list.copy():
                        if e.duration > permissions.max_song_length:
                            player.playlist.entries.remove(e)
                            entry_list.remove(e)
                            drop_count += 1
                            # Im pretty sure there's no situation where this would ever break
                            # Unless the first entry starts being played, which would make this a race condition
                    if drop_count:
                        print("Dropped %s songs" % drop_count)

                print("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                    listlen,
                    self._fixg(ttime),
                    ttime / listlen,
                    ttime / listlen - wait_per_song,
                    self._fixg(wait_per_song * num_songs))
                )

                await self.safe_delete_message(procmesg)

                if not listlen - drop_count:
                    raise exceptions.CommandError(
                        "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                        expire_in=30
                    )

                reply_text = "誰だ**%s**個も栗目を入れたクライミストは！！お陰で%s番目になっちまったよ:ginnan:"
                btext = str(listlen - drop_count)

            else:
                if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                    raise exceptions.PermissionsError(
                        "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                        expire_in=30
                    )

                try:
                    entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

                except exceptions.WrongEntryTypeError as e:
                    if e.use_url == song_url:
                        print("[Warning] Determined incorrect entry type, but suggested url is the same.  Help.")

                    if self.config.debug_mode:
                        print("[Info] Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                        print("[Info] Using \"%s\" instead" % e.use_url)

                    return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

                reply_text = " **%s**を登録したぞ<:terminus:286837427182764032>  この栗目は: %s"
                btext = entry.title

            if position == 1 and player.is_stopped:
                position = ':white_check_mark:すぐに再生されるよ！'
                reply_text %= (btext, position)

            else:
                try:
                    time_until = await player.playlist.estimate_time_until(position, player)
                    reply_text += ' :alarm_clock:%s後に再生される予定だァァァ！！'
                except:
                    traceback.print_exc()
                    time_until = ''

                reply_text %= (btext, position, time_until)

            return Response(reply_text, delete_after=30)

    async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_登録(player=player, channel=channel, author=author, permissions=permissions, leftover_args=leftover_args, song_url=song_url)


    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "%s個の栗目を処理中:arrows_counterclockwise: :innocent:" % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                print("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        print("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            self._fixg(ttime),
            ttime / num_songs,
            ttime / num_songs - wait_per_song,
            self._fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped for being too long."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("<:zakuro:310053103338651648>誰だ<:trump:245811283373326336>{}個も:chestnut: :eye: を入れたクライミストは<:nubesco:257184784344809473>！！お陰で{}秒もかかっちまったじゃねえか<:gaito88:257807307533058049>マジ<:ginnan:284978139350827009>".format(
            songs_added, self._fixg(ttime, 1)), delete_after=30)

    async def cmd_search(self, player, channel, author, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query

        Searches a service for a video and adds it to the queue.
        - service: any one of the following services:
            - youtube (yt) (default if unspecified)
            - soundcloud (sc)
            - yahoo (yh)
        - number: return a number of video results and waits for user to choose one
          - defaults to 1 if unspecified
          - note: If your search query starts with a number,
                  you must put your query in quotes
            - ex: {command_prefix}search 2 "I ran seagulls"
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your playlist item limit (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Searching for videos...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("No videos found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('{}{}'.format(self.config.subcommand_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n` or `exit`")
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("Ok nevermind.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.startswith(self.config.subcommand_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

                await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

                return Response("Alright, coming right up!", delete_after=30)
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Oh well :frowning:", delete_after=30)

    async def cmd_現在(self, player, channel, server, message):
        """
        Usage:
            {command_prefix}現在

        今再生している栗目を表示します
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "再生中:projector:： **%s**  :u7533:**%s** :alarm_clock:%s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
            else:
                np_text = "再生中:projector:： **%s** :alarm_clock:%s\n" % (player.current_entry.title, prog_str)

            ourl = player.current_entry.url
            if ourl.startswith("http://osu.ppy.sh/s/"):
                np_textn = "{}譜面のURL：{}".format(np_text, ourl)
            else:
                np_textn = "{}栗目のURL：{}".format(np_text, ourl)

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_textn)
            await self._manual_delete_check(message)
        else:
            return Response(
                'キューに何もありません。{}登録で栗目を追加できます。'.format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_np(self, player, channel, server, message):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_現在(player=player, channel=channel, server=server, message=message)

    async def cmd_ﾓ召喚(self, channel, author, voice_channel):
        """
        Usage:
            {command_prefix}ﾓ召喚

        ﾎﾓは全く関係ないですが栗目Botをサモンします（_何がサーモンをサモンじゃ！_）
        """

        if not author.voice_channel:
            raise exceptions.CommandError('いや、あんた今VCやってないでしょ？')

        voice_client = self.the_voice_clients.get(channel.server.id, None)
        if voice_client and voice_client.channel.server == author.voice_channel.server:
            await self.move_voice_client(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

        if not chperms.connect:
            self.safe_print("Cannot join channel \"%s\", no permission." % author.voice_channel.name)
            return Response(
                "```Cannot join channel \"%s\", no permission.```" % author.voice_channel.name,
                delete_after=25
            )

        elif not chperms.speak:
            self.safe_print("Will not join channel \"%s\", no permission to speak." % author.voice_channel.name)
            return Response(
                "```Will not join channel \"%s\", no permission to speak.```" % author.voice_channel.name,
                delete_after=25
            )

        player = await self.get_player(author.voice_channel, create=True)

        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_summon(self, channel, author, voice_channel):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_ﾓ召喚(channel=channel, author=author, voice_channel=voice_channel)

    async def cmd_待った(self, player):
        """
        Usage:
            {command_prefix}待った

        栗目で疲れたあなたに。再開を発行するまで待ってくれます
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError(':boom: なんすか？そもそも今うるさくないですよね（半ギレ', expire_in=30)

    async def cmd_pause(self, player):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        await self.cmd_待った(player=player)

    async def cmd_再開(self, player):
        """
        Usage:
            {command_prefix}再開

        待ったをかけたものの再生を再開します
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError(':boom:一時停止されていません', expire_in=30)

    async def cmd_resume(self, player):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        await self.cmd_再開(player=player)

    async def cmd_混ぜ(self, channel, player):
        """
        Usage:
            {command_prefix}混ぜ

        リストをごちゃ混ぜにします
        """

        player.playlist.shuffle()

        cards = [':spades:',':clubs:',':hearts:',':diamonds:']
        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(":ok_hand:", delete_after=15)

    async def cmd_shuffle(self, channel, player):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_混ぜ(channel=channel, player=player)

    async def cmd_リスト掃除(self, player, author):
        """
        Usage:
            {command_prefix}リスト掃除

        名前のとおりです
        """

        player.playlist.clear()
        return Response(':put_litter_in_its_place:', delete_after=20)

    async def cmd_clear(self, player, author):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_リスト掃除(player=player, author=author)

    async def cmd_ｲ次(self, player, channel, author, message, permissions, voice_channel):
        """
        Usage:
            {command_prefix}ｲ次

        あまりにも栗目なものが再生されたりして飛ばしたいときに発行して下さい
        投票や、オーナー権限で発動します
        """

        if player.is_stopped:
            raise exceptions.CommandError(":u7121:を飛ばせとは？", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    # print(player.playlist.peek()._waiting_futures[0].__dict__)
                    return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")

        if author.id == self.config.owner_id \
                or permissions.instaskip \
                or author == player.current_entry.meta.get('author', None):

            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(self.config.skips_required,
                              sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                ':heart:あなたの **{}** に対するスキップ投票を受理しました。'
                '\n:white_check_mark:投票の結果スキップが決定しました。{}'.format(
                    player.current_entry.title,
                    ':arrows_counterclockwize: 間もなく次の栗目が来ます！' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                ':heart:あなたの **{}** に対するスキップ投票を受理しました。'
                '\n:mega:あと**{}** {} スキップ投票が必要です'.format(
                    player.current_entry.title,
                    skips_remaining,
                    '人（あと一息！）の' if skips_remaining == 1 else '人（頑張って！）の'
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        await self.cmd_ｲ次(player=player, channel=channel, author=author, message=message, permissions=permissions, voice_channel=voice_channel)

    async def cmd_ﾜ度(self, message, player, new_volume=None):
        """
        Usage:
            {command_prefix}ﾜ度 (+/-)[音量]

        栗目Botの音量を変更します。1～100で指定して下さい
        ＋や－を入れると相対的な指定が可能です（現在の音量から[音量]上げる/下げる）
        """

        if not new_volume:
            return Response(':loudspeaker:現在のﾎﾜ度: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError(':boom:  {} は不正なﾎﾜ度です'.format(new_volume), expire_in=20)

        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response(':loudspeaker:ﾎﾜ度を%dから%dに、変更したドォォォン！！' % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume), expire_in=20)

    async def cmd_volume(self, message, player, new_volume=None):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_ﾜ度(message=message, player=player, new_volume=new_volume)

    async def cmd_リスト(self, channel, player):
        """
        Usage:
            {command_prefix}リスト

        リストに登録されたものを表示します
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... と %s 個の登録*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append("再生中:projector:: **%s** が追加した **%s** :alarm_clock:%s\n" % (
                     player.current_entry.meta['author'].name, player.current_entry.title, prog_str))
            else:
                lines.append("再生中:projector:: **%s** :alarm_clock:%s\n" % (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = ':hash:`{}` :film_frames:**{}**  :u7533:**{}**'.format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = ':hash:`{}` :film_frames:**{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*... そして %s つの栗目たち*' % unlisted)

        if not lines:
            lines.append(
                'リストに何もありませんよ？ なんか入れるには {}登録、または{}登録しよう。'.format(self.config.command_prefix, self.config.subcommand_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_queue(self, channel, player):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_リスト(channel=channel, player=player)

    async def cmd_ﾜﾎﾜｳﾙｾｰ(self, message, channel, server, author, search_range=50):
        """
        Usage:
            {command_prefix}ﾜﾎﾜｳﾙｾｰ [範囲]

        [範囲] で指定した数の栗目Botの発言を消します デフォルト: 50, 最大: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix, self.config.subcommand_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)

    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_ﾜﾎﾜｳﾙｾｰ(message=message, channel=channel, server=server, author=author, search_range=search_range)

    async def cmd_プレイリスト抽出(self, channel, song_url):
        """
        Usage:
            {command_prefix}プレイリスト抽出 URL

        Dumps the individual urls of a playlist
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await self.send_file(channel, fcontent, filename='playlist.txt', content="Here's the url dump for <%s>" % song_url)

        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_pldump(self, channel, song_url):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_プレイリスト抽出(channel=channel, song_url=song_url)

    async def cmd_id列挙(self, server, author, leftover_args, cat='全て'):
        """
        Usage:
            {command_prefix}id列挙 [カテゴリ]

        様々なIDを列挙します。 カテゴリは次のとおりです:
           全て, ユーザー, 役職, チャンネル
        """

        cats = ['チャンネル', '役職', 'ユーザー']

        if cat not in cats and cat != '全て':
            return Response(
                "利用可能なカテゴリ: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == '全て':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['あなたのID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'ユーザー':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == '役職':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'チャンネル':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='全て'):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_id列挙(server=server, author=author, leftover_args=leftover_args, cat=cat)

    async def cmd_権限(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}権限

        サーバーにいるユーザーの権限を表示します
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_perms(self, author, channel, server, permissions):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_権限(author=author, channel=channel, server=server, permissions=permissions)


    @owner_only
    async def cmd_ネーム変更(self, leftover_args, name):
        """
        Usage:
            {command_prefix}ネーム変更 名前

        ボットのユーザー名を更新します
        注意: この操作はDiscordの仕様上、1時間以内に複数回行うことはできません
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_ネーム変更(leftover_args=leftover_args, name=name)


    @owner_only
    async def cmd_命名(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}命名 名前

        Botのニックネームを変更します
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_命名(server=server, channel=channel, leftover_args=leftover_args, nick=nick)

    @owner_only
    async def cmd_アバター画像設定(self, message, url=None):
        """
        Usage:
            {プレフィックス}アバター画像設定 [URL]

        ボットのアバター画像を更新します。
        画像を添付することも可能で、その場合はURLを省略できます。
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("このアバターに変更できません: %s" % e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavater(self, message, url=None):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_アバター画像設定(message=message, url=url)


    async def cmd_切断(self, server):
        await self.disconnect_voice_client(server)
        return Response(":hear_no_evil:", delete_after=20)

    async def cmd_disconnect(self, server):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        return await self.cmd_切断(server=server)

    async def cmd_再起(self, channel):
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal

    async def cmd_restart(self, channel):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        await self.cmd_再起(channel=channel)

    async def cmd_あぼーん(self, channel):
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal

    async def cmd_shutdown(self, channel):
        """
        コマンドのオリジナル互換用ラッパエントリ。栗目ボットの日本語コマンドが使いづらい人用。
        """
        await self.cmd_あぼーん(channel=channel)

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            if not message_content.startswith(self.config.subcommand_prefix):
                return
            pass

        if message.author == self.user:
            self.safe_print("Ignoring command from myself (%s)" % message.content)
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return  # if I want to log this I just move it under the prefix check

        command, *args = message_content.split()  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        acommand = command[len(self.config.command_prefix):].lower().strip()
        subcommand = command[len(self.config.subcommand_prefix):].lower().strip()

        handler = getattr(self, 'cmd_%s' % acommand, None)
        subhandler = getattr(self, 'cmd_%s' % subcommand, None)
        if not handler and not subhandler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.send_message(message.channel, 'プライベートメッセージでの利用はできません。')
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            self.safe_print("[ブラックリストに登録されたユーザー] {0.id}/{0.name} ({1})".format(message.author, message_content))
            return

        else:
            self.safe_print("[コマンド] {0.id}/{0.name}が ({1})を実行".format(message.author, message_content))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler or subhandler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and acommand not in user_permissions.command_whitelist and subcommand not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            if handler == None:
                response = await subhandler(**handler_kwargs)
            else:
                response = await handler(**handler_kwargs)

            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            print("{0.__class__}: {0.message}".format(e))

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n%s\n```' % e.message,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def on_voice_state_update(self, before, after):
        if not all([before, after]):
            return

        if before.voice_channel == after.voice_channel:
            return

        if before.server.id not in self.players:
            return

        my_voice_channel = after.server.me.voice_channel  # This should always work, right?

        if not my_voice_channel:
            return

        if before.voice_channel == my_voice_channel:
            joining = False
        elif after.voice_channel == my_voice_channel:
            joining = True
        else:
            return  # Not my channel

        moving = before == before.server.me

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(my_voice_channel)

        if after == after.server.me and after.voice_channel:
            player.voice_client.channel = after.voice_channel

        if not self.config.auto_pause:
            return

        if sum(1 for m in my_voice_channel.voice_members if m != after.server.me):
            if auto_paused and player.is_paused:
                print("[自動一時停止機能] 一時停止解除")
                self.server_specific_data[after.server]['auto_paused'] = False
                player.resume()
        else:
            if not auto_paused and player.is_playing:
                print("[自動一時停止機能] 一時停止作動")
                self.server_specific_data[after.server]['auto_paused'] = True
                player.pause()

    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            self.safe_print("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)


if __name__ == '__main__':
    bot = MusicBot()
    bot.run()
