"""Bounded recovery for an occasional headless Chrome startup timeout."""
from __future__ import annotations
import subprocess
import sys
import time
from .common import LOG,PACKAGE,command


def run_motion(args,**kwargs):
    for attempt in range(2):
        try:
            return command(args,**kwargs)
        except subprocess.CalledProcessError as error:
            diagnostic=(error.stderr or '')+'\n'+(error.stdout or '')
            if attempt==0 and 'while trying to connect to the browser' in diagnostic:
                LOG.warning('Chrome did not start in time; retrying the motion command once.')
                time.sleep(2)
                continue
            LOG.error('Motion renderer failed: %s',diagnostic[-5000:])
            raise


if __name__=='__main__':
    run_motion(['node',str(PACKAGE/'motion/node_modules/@remotion/cli/remotion-cli.js'),*sys.argv[1:]],cwd=PACKAGE/'motion')
