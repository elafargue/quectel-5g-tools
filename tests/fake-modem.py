#!/usr/bin/env python3
"""A modem on a pty, for tests/test-cache-herd.

Answers the toolkit's read-only AT commands with the sample replies in
CLAUDE.md, DELAY seconds late, and appends every command it receives to
LOG -- which is the whole point: the test counts how many times each
question actually reached "the modem". LINK is where the pty's slave end
is symlinked, so the code under test opens it like any serial device.
"""
import os
import pty
import select
import time

DELAY = float(os.environ.get("DELAY", "0.2"))
LOG = os.environ["LOG"]
LINK = os.environ["LINK"]

REPLIES = {
    "ATI": "Quectel\r\nRM520N-GL\r\nRevision: RM520NGLAAR03A03M4G",
    "AT+GSN": "861234567890123",
    "AT+QSPN": '+QSPN: "I TIM","TIM","",0,"22201"',
    "AT+QCAINFO":
        '+QCAINFO: "PCC",275,75,"LTE BAND 1",1,280,-99,-14,-67,-4\r\n'
        '+QCAINFO: "SCC",1350,100,"LTE BAND 3",1,240,-95,-18,-68,-10,0,-,-\r\n'
        '+QCAINFO: "SCC",648768,10,"NR5G BAND 78",920',
    'AT+QENG="servingcell"':
        '+QENG: "servingcell","NOCONN"\r\n'
        '+QENG: "LTE","FDD",222,01,328261F,280,275,1,4,4,BE3,-99,-14,-66,7,4,30,-\r\n'
        '+QENG: "NR5G-NSA",222,01,920,-96,18,-10,648768,78,10,1',
    'AT+QENG="neighbourcell"':
        '+QENG: "neighbourcell intra","LTE",275,280,-14,-99,-67,-,-,-,-,-,-',
    "AT+QNWINFO": '+QNWINFO: "FDD LTE",22201,"LTE BAND 1",275',
}

master, slave = pty.openpty()
os.symlink(os.ttyname(slave), LINK)
buf = b""
while True:
    select.select([master], [], [])
    buf += os.read(master, 4096)
    while b"\r" in buf:
        line, buf = buf.split(b"\r", 1)
        cmd = line.strip().decode(errors="replace")
        if not cmd:
            continue
        with open(LOG, "a") as log:
            log.write(cmd + "\n")
        time.sleep(DELAY)
        reply = REPLIES.get(cmd)
        out = "\r\n%s\r\n\r\nOK\r\n" % reply if reply is not None else "\r\nOK\r\n"
        os.write(master, out.encode())
