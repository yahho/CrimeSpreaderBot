from __future__ import print_function

import os
import gc
import sys
import time
import traceback
import subprocess


class GIT(object):
    @classmethod
    def works(cls):
        try:
            return bool(subprocess.check_output('git --version', shell=True))
        except:
            return False


class PIP(object):
    @classmethod
    def run(cls, command, check_output=False):
        if not cls.works():
            raise RuntimeError("Could not import pip.")

        try:
            return PIP.run_python_m(*command.split(), check_output=check_output)
        except subprocess.CalledProcessError as e:
            return e.returncode
        except:
            traceback.print_exc()
            print("Error using -m method")

    @classmethod
    def run_python_m(cls, *args, **kwargs):
        check_output = kwargs.pop('check_output', False)
        check = subprocess.check_output if check_output else subprocess.check_call
        return check([sys.executable, '-m', 'pip'] + list(args))

    @classmethod
    def run_pip_main(cls, *args, **kwargs):
        import pip

        args = list(args)
        check_output = kwargs.pop('check_output', False)

        if check_output:
            from io import StringIO

            out = StringIO()
            sys.stdout = out

            try:
                pip.main(args)
            except:
                traceback.print_exc()
            finally:
                sys.stdout = sys.__stdout__

                out.seek(0)
                pipdata = out.read()
                out.close()

                print(pipdata)
                return pipdata
        else:
            return pip.main(args)

    @classmethod
    def run_install(cls, cmd, quiet=False, check_output=False):
        return cls.run("install %s%s" % ('-q ' if quiet else '', cmd), check_output)

    @classmethod
    def run_show(cls, cmd, check_output=False):
        return cls.run("show %s" % cmd, check_output)

    @classmethod
    def works(cls):
        try:
            import pip
            return True
        except ImportError:
            return False

    # noinspection PyTypeChecker
    @classmethod
    def get_module_version(cls, mod):
        try:
            out = cls.run_show(mod, check_output=True)

            if isinstance(out, bytes):
                out = out.decode()

            datas = out.replace('\r\n', '\n').split('\n')
            expectedversion = datas[3]

            if expectedversion.startswith('Version: '):
                return expectedversion.split()[1]
            else:
                return [x.split()[1] for x in datas if x.startswith("Version: ")][0]
        except:
            pass


def main():
    if not sys.version_info >= (3, 5):
        print("Python 3.5以上が必要だよん。 これは %sだよん。" % sys.version.split()[0])
        print("Python 3.5の検出を開始します...")

        pycom = None

        # Maybe I should check for if the current dir is the musicbot folder, just in case

        if sys.platform.startswith('win'):
            try:
                subprocess.check_output('py -3.5 -c "exit()"', shell=True)
                pycom = 'py -3.5'
            except:

                try:
                    subprocess.check_output('python3 -c "exit()"', shell=True)
                    pycom = 'python3'
                except:
                    pass

            if pycom:
                print("Python 3 が検出されました。  Botを起動しています...")
                os.system('start cmd /k %s run.py' % pycom)
                sys.exit(0)

        else:
            try:
                pycom = subprocess.check_output(['which', 'python3.5']).strip().decode()
            except:
                pass

            if pycom:
                print("\nPython 3 が検出されました。  Botを以下のコマンドで起動しています: ")
                print("  %s run.py\n" % pycom)

                os.execlp(pycom, pycom, 'run.py')

        print("python 3.5を利用してBotを起動して下さい。")
        input("Enterを押して続行する. . .")

        return

    import asyncio

    tried_requirementstxt = False
    tryagain = True

    loops = 0
    max_wait_time = 120

    while tryagain:
        # Maybe I need to try to import stuff first, then actually import stuff
        # It'd save me a lot of pain with all that awful exception type checking

        m = None
        try:
            from musicbot import MusicBot
            m = MusicBot()
            print("ログインしています・・・・", end='', flush=True)
            m.run()

        except SyntaxError:
            traceback.print_exc()
            break

        except ImportError as e:
            if not tried_requirementstxt:
                tried_requirementstxt = True

                # TODO: Better output
                print(e)
                print("依存関係の解決を開始しています...")

                err = PIP.run_install('--upgrade -r requirements.txt')

                if err:
                    print("\n依存関係の解決のためのインストールに必要です：%s" %
                          ['sudoを利用する', '管理者権限で実行する'][sys.platform.startswith('win')])
                    break
                else:
                    print("\n依存関係を解決しました。再度実行して正常に起動することを確認して下さい。\n")
            else:
                traceback.print_exc()
                print("不明なインポートです。終了します。")
                break

        except Exception as e:
            if hasattr(e, '__module__') and e.__module__ == 'musicbot.exceptions':
                if e.__class__.__name__ == 'HelpfulError':
                    print(e.message)
                    break

                elif e.__class__.__name__ == "TerminateSignal":
                    break

                elif e.__class__.__name__ == "RestartSignal":
                    loops = -1
            else:
                traceback.print_exc()

        finally:
            if not m or not m.init_ok:
                break

            asyncio.set_event_loop(asyncio.new_event_loop())
            loops += 1

        print("お掃除しています。。。", end=':ok_hand:')
        gc.collect()
        print("Done.")

        sleeptime = min(loops * 2, max_wait_time)
        if sleeptime:
            print("あと {} 秒で再起します・・・".format(loops*2))
            time.sleep(sleeptime)


if __name__ == '__main__':
    main()
