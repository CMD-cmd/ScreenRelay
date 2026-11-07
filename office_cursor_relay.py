"""Own a blocking native notification relay for authorized Office add-ins.

The native relay publishes appearance settings, observes local state files and
waits for requests. It reads no Office data or input events.
"""
import json
from pathlib import Path
import subprocess
import time


class OfficeCursorRelay:
    def __init__(self,directory,*,popen=subprocess.Popen,clock=time.monotonic):
        self.directory=Path(directory)
        self.helper=self.directory/'office-cursor-relay'
        self.popen,self.clock=popen,clock
        self.process=None
        self.retry_at=0
        self.last_error=None

    def authorized(self):
        try:
            value=json.loads((self.directory/'office-cursor-addon.json').read_text())
            return isinstance(value,dict) and value.get('installed') is True
        except (OSError,ValueError):return False

    def sync(self,active):
        # Stay listening while idle to answer a newly loaded add-in immediately.
        # The native helper reads the actual controller and remote-session state.
        if not self.authorized():
            self.close();return
        if self.process is not None:
            try:code=self.process.poll()
            except OSError as exc:self.last_error=str(exc);return
            if code is None:return
            self.process=None
            self.retry_at=self.clock()+60
            self.last_error=f'Office cursor relay exited ({code})'
        if self.clock()<self.retry_at:return
        try:
            if not self.helper.is_file():raise FileNotFoundError(str(self.helper))
            with (self.directory/'office-cursor-relay.log').open('ab') as log:
                self.process=self.popen([str(self.helper),'--directory',str(self.directory)],stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            self.last_error=None
        except OSError as exc:self.last_error=str(exc);self.retry_at=self.clock()+60

    def close(self):
        if self.process is None:return
        try:
            if self.process.poll() is None:
                self.process.terminate();self.process.wait(timeout=1)
            self.process=None
            self.last_error=None
        except subprocess.TimeoutExpired:self.last_error='Office cursor relay still stopping'
        except OSError as exc:self.last_error=str(exc)
