"""Frame reader: decodes the RTSP stream with ffmpeg into a shared-memory ring.

Runs as its own process so its pipe reads never wait on the pose service's GIL
(in-process reading lost frames whenever detection and pose were busy).
Usage: frame_reader.py <rtsp url> <shm name> <slots> <slot bytes>
Prints "size <w> <h>" once, then "<slot> <arrival time>" per frame on stdout.
"""
import fcntl
import subprocess
import sys
import time
from multiprocessing import resource_tracker, shared_memory


def main(url, shm_name, slots, slot_bytes):
    shm = shared_memory.SharedMemory(name=shm_name)
    resource_tracker.unregister(shm._name, "shared_memory")   # the pose service owns and unlinks it
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0", url],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except subprocess.TimeoutExpired:
        out = ""
    if not out:
        sys.exit(2)
    w, h = (int(v) for v in out.split(",")[:2])
    nbytes = w * h * 3 // 2   # yuv420p
    if nbytes > slot_bytes:
        print(f"frame {w}x{h} does not fit the shared-memory slot", file=sys.stderr)
        sys.exit(3)
    print(f"size {w} {h}", flush=True)

    proc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url, "-an", "-fps_mode", "passthrough",
                             "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"],
                            stdout=subprocess.PIPE, bufsize=0)
    try:   # a 1 MB pipe means a few reads per 3 MB frame instead of ~47
        fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)   # F_SETPIPE_SZ
    except OSError:
        pass
    i = 0
    while True:
        slot = i % slots
        view = shm.buf[slot * slot_bytes:slot * slot_bytes + nbytes]
        got = 0
        while got < nbytes:
            n = proc.stdout.readinto(view[got:])
            if not n:
                proc.kill()
                sys.exit(1)
            got += n
        view.release()
        print(f"{slot} {time.time():.3f}", flush=True)
        i += 1


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
