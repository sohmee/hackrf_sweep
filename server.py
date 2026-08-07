#!/usr/bin/env python3
"""
HackRF Sweep — by G4EA5  https://github.com/G4EA5/hackrf_sweep
Requirements: pip3 install flask flask-sock
              sudo apt install hackrf lsof psmisc
"""
import subprocess, json, time, os, signal, threading, queue
from flask import Flask, render_template_string
from flask_sock import Sock

app  = Flask(__name__)
sock = Sock(app)
MY_PID = os.getpid()

sweep_proc   = None
sweep_lock   = threading.Lock()
stdout_q     = queue.Queue(maxsize=8000)
stderr_q     = queue.Queue(maxsize=500)
# monotonically increasing sweep generation — used to discard stale stdout data
sweep_gen    = 0
dropped_lines = 0
drop_lock     = threading.Lock()
last_drop_warn = 0.0
# preserved across auto-restarts so crash recovery keeps bin width / gains
current_sweep = {"start": 88, "end": 108, "lna": 16, "vga": 20, "amp": 0, "binwidth": 100000}

def _note_drop(n=1):
    global dropped_lines
    with drop_lock:
        dropped_lines += n

def _take_drops():
    global dropped_lines
    with drop_lock:
        n = dropped_lines
        dropped_lines = 0
        return n

def _drain_stdout(proc, gen):
    """Thread: forward stdout only if this proc is still the current generation."""
    try:
        for line in proc.stdout:
            line = line.rstrip('\n')
            if not line: continue
            with sweep_lock:
                cur = sweep_gen
            if gen != cur:
                break          # this is a stale process, stop forwarding
            while True:
                with sweep_lock:
                    if gen != sweep_gen:
                        return
                try:
                    stdout_q.put(line, timeout=0.25)
                    break
                except queue.Full:
                    # Backpressure: drop oldest line so the stream stays live.
                    try:
                        stdout_q.get_nowait()
                        _note_drop()
                    except queue.Empty:
                        _note_drop()
    except Exception: pass

def _drain_stderr(proc, gen):
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if not line: continue
            with sweep_lock:
                cur = sweep_gen
            if gen != cur: break
            print(f"[hackrf] {line}")
            try: stderr_q.put_nowait(line)
            except queue.Full: pass
    except Exception: pass

def kill_hackrf_users():
    subprocess.call("sudo killall -9 hackrf_sweep 2>/dev/null", shell=True)
    try:
        r = subprocess.run(["sudo","lsof","-t","/dev/hackrf0"],
                           capture_output=True, text=True)
        for ps in r.stdout.strip().splitlines():
            try:
                pid = int(ps.strip())
                if pid != MY_PID: os.kill(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError): pass
    except Exception as e:
        print(f"[hackrf] lsof error: {e}")
    subprocess.call("sudo fuser -k /dev/hackrf0 2>/dev/null", shell=True)
    time.sleep(0.6)

def free_port(port):
    try:
        r = subprocess.run(["sudo","lsof","-ti",f"tcp:{port}"],
                           capture_output=True, text=True)
        for ps in r.stdout.strip().splitlines():
            try:
                pid = int(ps.strip())
                if pid != MY_PID: os.kill(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError): pass
    except Exception as e:
        print(f"[port] error: {e}")
    subprocess.call(f"sudo fuser -k {port}/tcp 2>/dev/null", shell=True)
    time.sleep(0.3)

def stop_sweep():
    global sweep_proc, sweep_gen
    with sweep_lock:
        proc = sweep_proc
        sweep_proc = None
        sweep_gen += 1          # invalidate all existing drain threads
    if proc:
        try: proc.kill(); proc.wait(timeout=2)
        except Exception: pass
    # drain queues — discard all stale lines
    for q in (stdout_q, stderr_q):
        try:
            while True: q.get_nowait()
        except queue.Empty: pass

def start_sweep(start, end, lna=16, vga=20, amp=0, binwidth=100000, force_kill=False):
    global sweep_proc, sweep_gen, current_sweep
    start    = max(1,    min(5980, int(start)))
    end      = max(21,   min(6000, int(end)))
    lna      = max(0,    min(40,   int(lna)))
    vga      = max(0,    min(62,   int(vga)))
    amp      = 1 if int(amp) else 0
    binwidth = max(50000, min(1000000, int(binwidth)))
    if end <= start: end = start + 20
    current_sweep = {"start": start, "end": end, "lna": lna, "vga": vga,
                     "amp": amp, "binwidth": binwidth}

    stop_sweep()           # kills old proc AND increments sweep_gen
    if force_kill:
        kill_hackrf_users()    # belt-and-braces — startup / stuck device only
    else:
        time.sleep(0.2)        # brief USB settle after normal stop

    cmd = ["hackrf_sweep",
           "-f", f"{start}:{end}",
           "-l", str(lna), "-g", str(vga), "-w", str(binwidth)]
    if amp: cmd += ["-a","1"]
    print(f"[sweep] {' '.join(cmd)}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    with sweep_lock:
        gen = sweep_gen          # capture current generation for this proc
        sweep_proc = proc

    threading.Thread(target=_drain_stdout, args=(proc,gen), daemon=True).start()
    threading.Thread(target=_drain_stderr, args=(proc,gen), daemon=True).start()
    return proc, start, end      # return actual clamped values

def restart_current_sweep(force_kill=False):
    """Restart hackrf_sweep with the last confirmed UI settings."""
    s = current_sweep
    return start_sweep(s["start"], s["end"], lna=s["lna"], vga=s["vga"],
                       amp=s["amp"], binwidth=s["binwidth"], force_kill=force_kill)

@sock.route('/ws')
def ws_route(ws):
    cur_start, cur_end = 88, 108
    proc = None
    started = False
    connect_t = time.time()

    def send_status(level, msg):
        try: ws.send(json.dumps({"type":"status","level":level,"msg":msg}))
        except Exception: pass

    def send_range(s, e, soft=False):
        """Tell browser the confirmed active frequency range after old proc is dead."""
        try:
            ws.send(json.dumps({
                "type": "range", "start": s, "end": e, "soft": soft,
                "binwidth": current_sweep["binwidth"],
            }))
        except Exception: pass

    def apply_sweep_cmd(d, first=False):
        nonlocal proc, cur_start, cur_end, started
        was_started = started
        prev = dict(current_sweep) if was_started else None
        s = max(1,   min(5980, int(float(d.get("start", 88)))))
        e = max(21,  min(6000, int(float(d.get("end",  108)))))
        proc, cur_start, cur_end = start_sweep(s, e,
            lna=d.get("lna",16), vga=d.get("vga",20),
            amp=d.get("amp",0), binwidth=d.get("binwidth",100000))
        started = True
        send_status("info", f"Sweep: {cur_start}–{cur_end} MHz")
        freq_changed = (not prev or prev.get("start") != cur_start
                        or prev.get("end") != cur_end)
        soft = was_started and not first and not freq_changed
        send_range(cur_start, cur_end, soft=soft)
        return time.time()

    last_stderr_flush = time.time()
    last_data_time    = time.time()

    while True:
        qsize = stdout_q.qsize()
        msg = None
        if qsize < 120:
            try:
                msg = ws.receive(timeout=0.002 if qsize > 40 else 0.01)
            except Exception:
                msg = None

        if msg:
            try:
                d = json.loads(msg)
                if d.get("cmd") == "setSweep":
                    last_data_time = apply_sweep_cmd(d, first=not started)
            except Exception as e2:
                send_status("error", f"Command error: {e2}")

        if not started and (time.time() - connect_t) > 1.5:
            proc, cur_start, cur_end = start_sweep(cur_start, cur_end, force_kill=True)
            started = True
            send_status("info", f"hackrf_sweep started {cur_start}–{cur_end} MHz")
            send_range(cur_start, cur_end, soft=False)
            last_data_time = time.time()

        now = time.time()
        if now - last_stderr_flush > 0.3:
            last_stderr_flush = now
            msgs = []
            try:
                while True: msgs.append(stderr_q.get_nowait())
            except queue.Empty: pass
            for ln in msgs:
                # Progress stats stay in the server terminal only — not the browser console.
                if "sweeps/second" in ln or "sweeps completed" in ln:
                    continue
                lvl = "error" if any(w in ln.lower() for w in
                    ["error","fail","unable","not found","no device",
                     "usage","open","board","couldn't transfer"]) else "warn"
                if lvl == "warn" and "caught signal" in ln.lower():
                    continue
                send_status(lvl, f"hackrf: {ln}")

        qsize = stdout_q.qsize()
        sent = 0
        # Decimate to the browser when the queue backs up (remote viewer on LAN).
        skip = 1 if qsize < 200 else (2 if qsize < 500 else 3)
        batch = min(400, max(40, qsize // skip + 1))
        skip_i = 0
        try:
            while sent < batch:
                line = stdout_q.get_nowait()
                skip_i += 1
                if skip_i % skip != 0:
                    continue
                try: ws.send(line + '\n')
                except Exception: return
                sent += 1
                last_data_time = time.time()
        except queue.Empty: pass

        drops = _take_drops()
        if drops:
            global last_drop_warn
            if now - last_drop_warn > 30:
                last_drop_warn = now
                send_status("warn",
                    f"Display backlog: skipped {drops} old sweep line(s). "
                    "This is normal with a remote browser — try 250 kHz bin width.")

        with sweep_lock:
            p = sweep_proc
        if p and p.poll() is not None:
            send_status("error",
                f"hackrf_sweep exited (rc={p.returncode}). Retrying in 3s…")
            time.sleep(3)
            proc, cur_start, cur_end = restart_current_sweep(force_kill=True)
            send_status("info","hackrf_sweep restarted")
            send_range(cur_start, cur_end, soft=True)
            last_data_time = time.time()
        elif sent == 0 and (time.time() - last_data_time) > 8:
            send_status("warn","No data for 8s — restarting…")
            proc, cur_start, cur_end = restart_current_sweep(force_kill=True)
            send_status("info","hackrf_sweep restarted (stall)")
            send_range(cur_start, cur_end, soft=True)
            last_data_time = time.time()

# ─────────────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>HackRF Sweep</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090e;--panel:#0c0f16;--b1:#1e3020;--b2:#243828;
  --acc:#00ff88;--a2:#00d4ff;--a3:#ffcc00;
  --dim:#7aaa8a;--text:#d8f0d8;--danger:#ff4455;--warn:#ffaa22;
  --cbg:#080e0a;
}
body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;
     font-size:15px;display:flex;flex-direction:column;height:100vh;overflow:hidden}

/* ── TOP BAR ── */
#topbar{display:flex;align-items:center;gap:0;background:var(--panel);
        border-bottom:2px solid var(--b2);flex-shrink:0;height:54px;overflow-x:auto}
#topbar::-webkit-scrollbar{height:3px}
#topbar::-webkit-scrollbar-thumb{background:var(--b2)}
#title{font-family:'Orbitron',sans-serif;font-size:17px;font-weight:900;
       color:var(--acc);letter-spacing:3px;padding:0 16px;
       text-shadow:0 0 16px #00ff8855;white-space:nowrap;flex-shrink:0}
.tsep{width:1px;background:var(--b2);align-self:stretch;margin:8px 0;flex-shrink:0}
.tg{display:flex;align-items:center;gap:7px;padding:0 12px;flex-shrink:0}
.tg label{color:var(--dim);font-size:13px;white-space:nowrap}

/* freq inputs */
.freq-inp{width:76px;height:36px;background:var(--cbg);border:1.5px solid var(--b2);
          color:var(--acc);padding:0 8px;font-family:'Share Tech Mono',monospace;
          font-size:20px;border-radius:4px;outline:none;transition:border-color .15s}
.freq-inp:focus{border-color:var(--acc)}
.freq-inp.pending{border-color:var(--warn)!important}
/* end freq display — bigger */
#end-display{font-size:20px;color:var(--a2);font-weight:bold;min-width:54px}

#span-sel{height:36px;background:var(--cbg);border:1.5px solid var(--b2);
          color:var(--a2);font-family:'Share Tech Mono',monospace;font-size:14px;
          border-radius:4px;outline:none;cursor:pointer;padding:0 6px;min-width:96px}
#span-sel:focus{border-color:var(--a2)}

#sweep-btn{height:38px;padding:0 16px;background:transparent;border:2px solid var(--acc);
           color:var(--acc);font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;
           letter-spacing:2px;cursor:pointer;border-radius:4px;
           transition:background .15s,color .15s,box-shadow .15s;white-space:nowrap}
#sweep-btn:hover{background:var(--acc);color:#000;box-shadow:0 0 16px #00ff8844}

/* top sliders */
.ts{display:flex;align-items:center;gap:5px}
.ts input[type=range]{width:72px;accent-color:var(--a2);cursor:pointer}
.ts .tv{color:var(--a2);font-size:14px;min-width:44px}

/* mode / action buttons */
.mbtn{height:32px;padding:0 11px;background:transparent;border:1.5px solid var(--dim);
      color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:14px;
      cursor:pointer;border-radius:4px;transition:all .15s;white-space:nowrap}
.mbtn.on{border-color:var(--a2);color:var(--a2);background:rgba(0,212,255,.08)}
.mbtn:hover{border-color:var(--text);color:var(--text)}
.abtn{height:32px;padding:0 10px;background:transparent;border:1.5px solid var(--b2);
      color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:13px;
      cursor:pointer;border-radius:4px;transition:all .15s;white-space:nowrap}
.abtn:hover{border-color:var(--a3);color:var(--a3)}
.abtn.act{border-color:var(--a2);color:var(--a2);background:rgba(0,212,255,.08)}

/* status */
#sdot{width:10px;height:10px;border-radius:50%;background:var(--dim);flex-shrink:0;transition:background .3s}
#sdot.ok{background:var(--acc);box-shadow:0 0 7px var(--acc)}
#sdot.err{background:var(--danger);box-shadow:0 0 7px var(--danger)}
#stxt{color:var(--dim);font-size:13px;white-space:nowrap}
#device-info{color:#447755;font-size:11px;white-space:nowrap;max-width:200px;
             overflow:hidden;text-overflow:ellipsis}

/* ── BODY ROW ── */
#body-row{display:flex;flex:1;overflow:hidden;min-height:0}

/* ── LEFT PANEL ── */
#left-panel{width:210px;flex-shrink:0;background:var(--panel);
            border-right:2px solid var(--b2);display:flex;flex-direction:column;
            overflow-y:auto;overflow-x:hidden}
#left-panel::-webkit-scrollbar{width:4px}
#left-panel::-webkit-scrollbar-thumb{background:var(--b2)}

/* ── RIGHT PANEL ── */
#right-panel{width:210px;flex-shrink:0;background:var(--panel);
             border-left:2px solid var(--b2);display:flex;flex-direction:column;
             overflow-y:auto;overflow-x:hidden}
#right-panel::-webkit-scrollbar{width:4px}
#right-panel::-webkit-scrollbar-thumb{background:var(--b2)}

/* panel sections */
.ps{padding:11px 13px 9px;border-bottom:1px solid var(--b1)}
.pst{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:700;
     color:var(--dim);letter-spacing:2px;margin-bottom:9px;text-transform:uppercase}
.si{display:flex;flex-direction:column;gap:5px;margin-bottom:9px}
.si:last-child{margin-bottom:0}
.sl{display:flex;justify-content:space-between;align-items:center}
.sl .ln{color:var(--text);font-size:13px;display:flex;align-items:center;gap:4px}
.sl .lv{color:var(--a2);font-size:15px;font-weight:bold;min-width:44px;text-align:right}
input[type=range]{width:100%;height:6px;cursor:pointer;accent-color:var(--a2);border-radius:3px}
.psel{width:100%;height:32px;background:var(--cbg);border:1.5px solid var(--b2);
      color:var(--text);font-family:'Share Tech Mono',monospace;font-size:13px;
      border-radius:4px;outline:none;cursor:pointer;padding:0 6px}
.psel:focus{border-color:var(--a2)}
.pbtn{width:100%;height:32px;margin-bottom:5px;background:transparent;
      border:1.5px solid var(--b2);color:var(--dim);font-family:'Share Tech Mono',monospace;
      font-size:13px;cursor:pointer;border-radius:4px;transition:all .15s;
      text-align:left;padding:0 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pbtn:last-child{margin-bottom:0}
.pbtn:hover{border-color:var(--a3);color:var(--a3)}
.pbtn.act{border-color:var(--a2);color:var(--a2);background:rgba(0,212,255,.08)}
.pbtn.qact{border-color:var(--acc);color:var(--acc);background:rgba(0,255,136,.08)}

/* signal strength meter */
#sig-meter-wrap{padding:0 13px 10px}
.sig-bar-bg{height:8px;background:#0a120a;border-radius:4px;margin-top:4px;overflow:hidden}
.sig-bar-fg{height:100%;border-radius:4px;transition:width .15s;background:linear-gradient(90deg,#00d4ff,#00ff88,#ffcc00,#ff4455)}

/* peak readout */
#peak-readout{font-size:20px;color:var(--a2);font-weight:bold;padding:4px 0 0}
#peak-freq{font-size:11px;color:var(--dim)}

/* ── DISPLAY ── */
#display{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#freq-ruler{height:30px;background:var(--panel);border-bottom:1px solid var(--b1);flex-shrink:0}
#freq-ruler canvas{display:block}
#spectrum-wrap{height:150px;flex-shrink:0;position:relative;
               background:#040a04;border-bottom:1px solid var(--b1)}
#spectrum-canvas{display:block;width:100%;height:100%}
#db-axis{position:absolute;left:0;top:0;bottom:0;width:40px;
         display:flex;flex-direction:column;justify-content:space-between;
         padding:3px 4px;color:#6a9a6a;font-size:11px;
         pointer-events:none;z-index:2;text-shadow:0 0 4px #000}
#waterfall-wrap{flex:1;position:relative;overflow:hidden;cursor:crosshair}
#waterfall{display:block}
#marker-tip{position:absolute;background:#0a1820;border:1px solid var(--a2);
            color:var(--text);font-size:13px;padding:4px 8px;border-radius:4px;
            pointer-events:none;display:none;z-index:20;white-space:nowrap}

/* ── INFO BAR ── */
#infobar{display:flex;align-items:center;gap:0;background:var(--panel);
         border-top:2px solid var(--b2);flex-shrink:0;height:40px;
         padding:0 10px;overflow:hidden}
.ib{padding:0 12px;border-right:1px solid var(--b1);white-space:nowrap;
    height:100%;display:flex;align-items:center;gap:5px}
.ib:last-child{border-right:none;margin-left:auto}
.ib .ik{color:var(--dim);font-size:13px}
.ib .iv{color:var(--a2);font-size:17px;font-weight:bold}
#kbd-hint{color:#2a4a2a;font-size:11px;text-align:right}

/* ── CONSOLE ── */
#console-wrap{background:#060810;border-top:1px solid #1a1228;
              flex-shrink:0;display:flex;flex-direction:column;
              height:88px;transition:height .2s}
#console-wrap.collapsed{height:24px}
#console-header{display:flex;align-items:center;gap:8px;padding:3px 12px;
                background:#0c0f18;border-bottom:1px solid #1a1228;
                cursor:pointer;flex-shrink:0;user-select:none}
#console-header span{font-size:12px;color:var(--dim)}
#cbadge{font-size:10px;padding:1px 6px;border-radius:8px;background:#1a1228;
        color:var(--warn);display:none}
#cbadge.has-err{background:#2a0810;color:var(--danger);display:inline}
#cbadge.has-warn{background:#1a1008;color:var(--warn);display:inline}
#ctoggle{margin-left:auto;font-size:11px;color:var(--dim)}
#console-log{flex:1;overflow-y:auto;padding:3px 12px;font-size:12px;line-height:1.6}
#console-wrap.collapsed #console-log{display:none}
.log-info{color:#4a9968}.log-warn{color:var(--warn)}.log-error{color:var(--danger)}
#console-log::-webkit-scrollbar{width:4px}
#console-log::-webkit-scrollbar-thumb{background:var(--b1)}

/* ── HELP TOOLTIP ── */
.hi{display:inline-flex;align-items:center;justify-content:center;
    width:16px;height:16px;border-radius:50%;background:var(--b2);
    color:var(--dim);font-size:10px;cursor:help;font-style:normal;
    border:1px solid #334433;flex-shrink:0;position:relative}
/* tooltip — smart positioning via JS, default right */
.tt{display:none;position:fixed;background:#0a1820;border:1px solid var(--a2);
    color:var(--text);font-size:12px;line-height:1.7;padding:10px 14px;
    border-radius:5px;width:230px;z-index:9999;pointer-events:none;
    box-shadow:0 4px 20px rgba(0,0,0,.85);white-space:normal;
    /* will be positioned by JS on hover */}
.tt strong{color:var(--a2);display:block;margin-bottom:3px;font-size:13px}
.hi:hover .tt{display:block}

/* ── MODALS ── */
.modal{display:none;position:fixed;inset:0;z-index:10000;
       background:rgba(0,0,0,.78);align-items:center;justify-content:center}
.modal.open{display:flex}
.mbox{background:#0c1018;border:1px solid var(--a2);border-radius:8px;
      width:600px;max-width:96vw;max-height:88vh;overflow-y:auto;
      padding:26px 30px;box-shadow:0 8px 40px rgba(0,0,0,.85);
      font-size:14px;line-height:1.8}
.mbox::-webkit-scrollbar{width:5px}
.mbox::-webkit-scrollbar-thumb{background:var(--b2)}
.mbox h2{font-family:'Orbitron',sans-serif;color:var(--a2);font-size:16px;
          margin-bottom:18px;letter-spacing:2px}
.mbox h3{color:var(--acc);font-size:12px;letter-spacing:1px;margin:18px 0 7px;
          text-transform:uppercase;font-family:'Orbitron',sans-serif}
.mbox p{color:var(--text);margin-bottom:7px}
.mcl{float:right;background:transparent;border:1px solid var(--dim);color:var(--dim);
     padding:3px 12px;cursor:pointer;border-radius:3px;font-family:inherit;font-size:12px}
.mcl:hover{border-color:var(--danger);color:var(--danger)}
.hr{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--b1)}
.hr:last-child{border-bottom:none}
.hk{color:var(--a2);min-width:110px;font-size:12px;flex-shrink:0}
.hd{color:var(--text);font-size:13px}
code{background:#0a1410;color:var(--acc);padding:1px 6px;border-radius:3px;
     font-family:'Share Tech Mono',monospace;font-size:12px}
.tag{display:inline-block;background:var(--b2);color:var(--a2);padding:1px 6px;
     border-radius:3px;font-size:12px;margin:1px}
.os-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:9px 0}
.os-card{background:#0a1410;border:1px solid var(--b2);border-radius:4px;padding:9px 11px}
.os-card .osn{color:var(--acc);font-size:12px;margin-bottom:4px;font-family:'Orbitron',sans-serif}
.os-card p{color:var(--dim);font-size:12px;margin:0}
.gd{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.gd.ok{background:var(--acc)}.gd.warn{background:var(--warn)}

/* settings modal */
#settings-modal .mbox{width:480px}
.srow{display:flex;justify-content:space-between;align-items:center;
      padding:8px 0;border-bottom:1px solid var(--b1);font-size:13px}
.srow:last-child{border-bottom:none}
.srow label{color:var(--dim)}
.srow .sv{color:var(--text)}

/* FM listen */
#fm-bar{background:var(--panel);border-top:1px solid var(--b2);
        padding:6px 14px;display:flex;align-items:center;gap:12px;
        font-size:13px;flex-shrink:0;display:none}
#fm-bar.visible{display:flex}
#fm-freq-display{color:var(--acc);font-size:18px;font-weight:bold;min-width:90px}
#fm-status{color:var(--dim);font-size:12px}
.fmbtn{height:30px;padding:0 12px;background:transparent;border:1.5px solid var(--b2);
       color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:13px;
       cursor:pointer;border-radius:4px;transition:all .15s}
.fmbtn:hover{border-color:var(--acc);color:var(--acc)}
.fmbtn.active{border-color:var(--danger);color:var(--danger)}
</style>
</head>
<body>

<!-- ═══════════════════ TOP BAR ═══════════════════ -->
<div id="topbar">
  <div id="title">⬡ HACKRF</div>
  <div class="tsep"></div>

  <div class="tg">
    <label>START</label>
    <input type="number" class="freq-inp" id="start" value="88" min="1" max="5980" step="1">
    <label style="color:#447755;font-size:12px">MHz</label>
  </div>
  <div class="tg" style="padding-left:4px">
    <label>SPAN</label>
    <select id="span-sel" onchange="onSpanChange()">
      <option value="20">20 MHz</option>
      <option value="40">40 MHz</option>
      <option value="60">60 MHz</option>
      <option value="80">80 MHz</option>
      <option value="100">100 MHz</option>
      <option value="160">160 MHz</option>
      <option value="200">200 MHz</option>
      <option value="260">260 MHz</option>
      <option value="500">500 MHz</option>
      <option value="760">760 MHz</option>
      <option value="1000">1000 MHz</option>
      <option value="1500">1500 MHz</option>
      <option value="1760">1760 MHz</option>
      <option value="2000">2000 MHz</option>
      <option value="3000">3000 MHz</option>
      <option value="3500">3500 MHz</option>
      <option value="4000">4000 MHz</option>
      <option value="4500">4500 MHz</option>
      <option value="5000">5000 MHz</option>
      <option value="5500">5500 MHz</option>
      <option value="5980">5980 MHz</option>
    </select>
    <label style="color:#447755;font-size:13px">END:</label>
    <span id="end-display">108</span>
    <label style="color:#447755;font-size:12px">MHz</label>
  </div>
  <div class="tg" style="padding-left:6px">
    <button id="sweep-btn" onclick="setSweep()">▶ SWEEP</button>
  </div>
  <div class="tsep"></div>

  <div class="tg">
    <label>LNA</label>
    <div class="ts">
      <input type="range" id="lna" min="0" max="40" step="8" value="16"
             oninput="onGainSlider('lna',this.value,'dB')">
      <span class="tv" id="lna-val">16dB</span>
    </div>
  </div>
  <div class="tg">
    <label>VGA</label>
    <div class="ts">
      <input type="range" id="vga" min="0" max="62" step="2" value="20"
             oninput="onGainSlider('vga',this.value,'dB')">
      <span class="tv" id="vga-val">20dB</span>
    </div>
  </div>
  <div class="tg">
    <label>AMP</label>
    <div class="ts">
      <input type="range" id="amp" min="0" max="1" step="1" value="0"
             oninput="onAmpSlider(this.value)">
      <span class="tv" id="amp-val">OFF</span>
    </div>
  </div>
  <div class="tsep"></div>

  <div class="tg" style="gap:6px">
    <button class="mbtn on" id="avg-btn" onclick="toggleAvg()">AVG</button>
    <button class="mbtn" id="peak-btn" onclick="togglePeak()">PEAK</button>
  </div>
  <div class="tsep"></div>

  <div class="tg" style="gap:5px">
    <button class="abtn" onclick="autoScale()">⚡AUTO</button>
    <button class="abtn" id="marker-mode-btn" onclick="toggleMarkerMode()">◈MARK</button>
    <button class="abtn" onclick="exportPNG()">↓PNG</button>
    <button class="abtn" onclick="openModal('fm-modal')">📻FM</button>
    <button class="abtn" onclick="openModal('settings-modal')">💾SETTINGS</button>
    <button class="abtn" onclick="openModal('help-modal')">?HELP</button>
    <button class="abtn" onclick="openModal('req-modal')">⚙REQ</button>
  </div>
  <div class="tsep"></div>

  <div class="tg" style="margin-left:auto;gap:8px">
    <div id="sdot"></div>
    <span id="stxt">disconnected</span>
    <span id="device-info"></span>
  </div>
</div>

<!-- ═══════════════════ BODY ROW ═══════════════════ -->
<div id="body-row">

<!-- LEFT PANEL: display + colour + signal -->
<div id="left-panel">
  <div class="ps">
    <div class="pst">Display</div>
    <div class="si">
      <div class="sl">
        <span class="ln">MIN dB <i class="hi">?<span class="tt"><strong>Noise Floor</strong>Bottom of colour scale. Typical: –100 to –85 dBm. Keys [ and ]</span></i></span>
        <span class="lv" id="mindb-val">–100</span>
      </div>
      <input type="range" id="mindb" min="-120" max="-40" step="5" value="-100"
             oninput="onDisplaySlider('mindb',this.value)">
    </div>
    <div class="si">
      <div class="sl">
        <span class="ln">MAX dB <i class="hi">?<span class="tt"><strong>Peak Level</strong>Top of colour scale. FM: –50 to –20 dBm. Keys + and -</span></i></span>
        <span class="lv" id="maxdb-val">–20</span>
      </div>
      <input type="range" id="maxdb" min="-80" max="0" step="5" value="-20"
             oninput="onDisplaySlider('maxdb',this.value)">
    </div>
    <div class="si">
      <div class="sl">
        <span class="ln">BIN W <i class="hi">?<span class="tt"><strong>Bin Width</strong>Frequency resolution per sample. 100k ideal for FM. Narrower=more detail.</span></i></span>
        <span class="lv" id="bw-val">100k</span>
      </div>
      <input type="range" id="binwidth" min="50000" max="500000" step="50000" value="100000"
             oninput="onBinwidthSlider(this.value)">
    </div>
    <div class="si">
      <div class="sl">
        <span class="ln">WF SPD <i class="hi">?<span class="tt"><strong>Waterfall Speed</strong>Sweeps combined per row. Higher=cleaner slower scroll. 4–8 good for FM.</span></i></span>
        <span class="lv" id="wfspeed-val">4</span>
      </div>
      <input type="range" id="wfspeed" min="1" max="16" step="1" value="4"
             oninput="wfSpeed=+this.value;document.getElementById('wfspeed-val').textContent=this.value">
    </div>
  </div>

  <div class="ps">
    <div class="pst">Colour Scheme</div>
    <select class="psel" id="scheme-select" onchange="setScheme(this.value)">
      <option value="classic">Classic</option>
      <option value="grayscale">Grayscale</option>
      <option value="nightvision">Night Vision</option>
      <option value="hot">Hot</option>
      <option value="viridis">Viridis</option>
    </select>
  </div>

  <div class="ps">
    <div class="pst">Signal Monitor</div>
    <div style="font-size:12px;color:var(--dim);margin-bottom:4px">Peak Power</div>
    <div id="peak-readout">---</div>
    <div id="peak-freq">---</div>
    <div style="font-size:12px;color:var(--dim);margin:8px 0 4px">Signal Level</div>
    <div class="sig-bar-bg"><div class="sig-bar-fg" id="sig-bar" style="width:0%"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-top:2px">
      <span>WEAK</span><span>STRONG</span>
    </div>
  </div>

  <div class="ps" style="border-bottom:none">
    <div class="pst">Actions</div>
    <button class="pbtn" onclick="factoryDefaults()">↺ Factory Defaults</button>
    <button class="pbtn danger" onclick="clearMarkers()" style="border-color:var(--danger);color:var(--danger)" id="clear-markers-btn">✕ Clear Markers</button>
  </div>
</div>

<!-- CENTRE DISPLAY -->
<div id="display">
  <div id="freq-ruler"><canvas id="ruler-canvas"></canvas></div>
  <div id="spectrum-wrap">
    <div id="db-axis">
      <span id="db-top"></span><span></span>
      <span id="db-mid"></span><span></span>
      <span id="db-bot"></span>
    </div>
    <canvas id="spectrum-canvas"></canvas>
  </div>
  <div id="waterfall-wrap">
    <canvas id="waterfall"></canvas>
    <div id="marker-tip"></div>
  </div>
  <!-- FM listen bar — shown when listening -->
  <div id="fm-bar">
    <span style="color:var(--dim);font-size:13px">📻 LISTENING:</span>
    <span id="fm-freq-display">---</span>
    <span id="fm-status">Use FM modal to tune</span>
    <button class="fmbtn active" onclick="stopFM()" style="margin-left:auto">■ STOP</button>
  </div>
  <div id="infobar">
    <div class="ib"><span class="ik">FREQ</span><span class="iv" id="cursor-freq">---</span><span class="ik">MHz</span></div>
    <div class="ib"><span class="ik">PWR</span><span class="iv" id="cursor-power">---</span><span class="ik">dBm</span></div>
    <div class="ib"><span class="ik">SPAN</span><span class="iv" id="info-span">---</span><span class="ik">MHz</span></div>
    <div class="ib"><span class="ik">BINS</span><span class="iv" id="info-bins">---</span></div>
    <div class="ib"><span class="ik">RATE</span><span class="iv" id="info-rate">---</span><span class="ik">/s</span></div>
    <div class="ib"><span id="kbd-hint">Spc=sweep P=peak A=avg M=mark X=clear S=png +=max -=min [/]=min</span></div>
  </div>
  <div id="console-wrap">
    <div id="console-header" onclick="toggleConsole()">
      <span>CONSOLE</span><span id="cbadge"></span><span id="ctoggle">▲</span>
    </div>
    <div id="console-log"></div>
  </div>
</div>

<!-- RIGHT PANEL: quick bands + markers -->
<div id="right-panel">
  <div class="ps">
    <div class="pst">Quick Bands <i class="hi">?<span class="tt"><strong>Quick Bands</strong>Click to jump straight to that band. Active band is highlighted green.</span></i></div>
    <button class="pbtn" id="qb-fm"     onclick="quickBand('qb-fm',    87,  20)">📻 FM Broadcast</button>
    <button class="pbtn" id="qb-dab"    onclick="quickBand('qb-dab',   174, 20)">📻 DAB Digital</button>
    <button class="pbtn" id="qb-air"    onclick="quickBand('qb-air',   118, 20)">✈ Aviation VHF</button>
    <button class="pbtn" id="qb-adsb"   onclick="quickBand('qb-adsb',  1080,20)">✈ ADS-B 1090</button>
    <button class="pbtn" id="qb-marine" onclick="quickBand('qb-marine',156, 20)">⚓ Marine VHF</button>
    <button class="pbtn" id="qb-2m"     onclick="quickBand('qb-2m',    144, 20)">📡 2m Amateur</button>
    <button class="pbtn" id="qb-70cm"   onclick="quickBand('qb-70cm',  430, 20)">📡 70cm Amateur</button>
    <button class="pbtn" id="qb-pmr"    onclick="quickBand('qb-pmr',   446, 20)">🔑 PMR446</button>
    <button class="pbtn" id="qb-gsm"    onclick="quickBand('qb-gsm',   860, 40)">📶 GSM 900</button>
    <button class="pbtn" id="qb-lte"    onclick="quickBand('qb-lte',   1800,40)">📶 LTE 1800</button>
    <button class="pbtn" id="qb-gps"    onclick="quickBand('qb-gps',   1560,20)">🛰 GPS L1</button>
    <button class="pbtn" id="qb-noaa"   onclick="quickBand('qb-noaa',  136, 20)">🌤 NOAA Weather</button>
    <button class="pbtn" id="qb-wifi24" onclick="quickBand('qb-wifi24',2400,100)">📶 WiFi 2.4G</button>
    <button class="pbtn" id="qb-wifi5"  onclick="quickBand('qb-wifi5', 5160,500)">📶 WiFi 5G</button>
    <button class="pbtn" id="qb-irid"   onclick="quickBand('qb-irid',  1616,20)">🛰 Iridium Sat</button>
    <button class="pbtn" id="qb-full"   onclick="quickBand('qb-full',  1,  5980)">🌍 Full Sweep</button>
  </div>

  <div class="ps">
    <div class="pst">Markers</div>
    <button class="pbtn" id="mrk-btn" onclick="toggleMarkerMode()">◈ MARKERS: OFF</button>
    <div id="marker-list" style="margin-top:7px;font-size:12px;color:var(--dim)">No markers</div>
  </div>
</div>

</div><!-- end body-row -->

<!-- ═══════════ FM LISTEN MODAL ═══════════ -->
<div id="fm-modal" class="modal" onclick="modalBackdrop(event,this)">
<div class="mbox" style="width:420px">
  <button class="mcl" onclick="closeModal('fm-modal')">✕ CLOSE</button>
  <h2>📻 FM Demodulator</h2>
  <p>Listen to an FM broadcast station. Click on a station in the waterfall or spectrum first (with marker mode ON) to mark a frequency, then type it below, or hover and use the cursor frequency.</p>
  <p style="color:var(--warn);font-size:12px;margin-top:8px">⚠ Requires <code>hackrf_transfer</code> and <code>sox</code> on the server. FM listening uses a separate process — the sweep will continue running alongside it.</p>
  <div style="margin:16px 0;display:flex;gap:10px;align-items:center">
    <label style="color:var(--dim);font-size:13px">Frequency (MHz):</label>
    <input type="number" id="fm-tune-freq" value="100.0" step="0.1" min="87.5" max="108"
           style="width:90px;height:34px;background:var(--cbg);border:1.5px solid var(--b2);
                  color:var(--acc);padding:0 8px;font-family:'Share Tech Mono',monospace;
                  font-size:16px;border-radius:4px;outline:none">
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <button onclick="startFM()" style="height:36px;padding:0 16px;background:transparent;
      border:2px solid var(--acc);color:var(--acc);font-family:'Orbitron',sans-serif;
      font-size:12px;cursor:pointer;border-radius:4px">▶ LISTEN</button>
    <button onclick="stopFM()" style="height:36px;padding:0 16px;background:transparent;
      border:2px solid var(--danger);color:var(--danger);font-family:'Orbitron',sans-serif;
      font-size:12px;cursor:pointer;border-radius:4px">■ STOP</button>
    <button onclick="recordFM()" style="height:36px;padding:0 16px;background:transparent;
      border:2px solid var(--warn);color:var(--warn);font-family:'Orbitron',sans-serif;
      font-size:12px;cursor:pointer;border-radius:4px">⏺ RECORD</button>
  </div>
  <div id="fm-modal-status" style="margin-top:12px;font-size:12px;color:var(--dim)">Not listening</div>
  <h3>How it works</h3>
  <p>This sends a command to the server to run <code>hackrf_transfer</code> piped through <code>sox</code> for FM demodulation. The sweep will keep running. You can tune any FM station between 87.5–108 MHz. Signal quality depends on your antenna.</p>
  <p>Server-side command used:<br>
  <code>hackrf_transfer -r - -f [FREQ_HZ] -s 2000000 | sox ...</code></p>
</div></div>

<!-- ═══════════ SETTINGS MODAL ═══════════ -->
<div id="settings-modal" class="modal" onclick="modalBackdrop(event,this)">
<div class="mbox" id="settings-modal-box" style="width:460px">
  <button class="mcl" onclick="closeModal('settings-modal')">✕ CLOSE</button>
  <h2>💾 Settings</h2>
  <h3>Save &amp; Load</h3>
  <div style="display:flex;gap:10px;margin-bottom:14px">
    <button onclick="saveSettings()" style="height:34px;padding:0 14px;background:transparent;
      border:1.5px solid var(--acc);color:var(--acc);font-family:'Share Tech Mono',monospace;
      font-size:13px;cursor:pointer;border-radius:4px">💾 Save Settings</button>
    <button onclick="loadSettings()" style="height:34px;padding:0 14px;background:transparent;
      border:1.5px solid var(--a2);color:var(--a2);font-family:'Share Tech Mono',monospace;
      font-size:13px;cursor:pointer;border-radius:4px">📂 Load Settings</button>
    <button onclick="factoryDefaults()" style="height:34px;padding:0 14px;background:transparent;
      border:1.5px solid var(--warn);color:var(--warn);font-family:'Share Tech Mono',monospace;
      font-size:13px;cursor:pointer;border-radius:4px">↺ Factory Defaults</button>
  </div>
  <h3>Current Values</h3>
  <div id="settings-preview" style="font-size:12px;color:var(--dim);line-height:1.8"></div>
  <div id="settings-status" style="margin-top:10px;font-size:12px;color:var(--acc)"></div>
</div></div>

<!-- ═══════════ HELP MODAL ═══════════ -->
<div id="help-modal" class="modal" onclick="modalBackdrop(event,this)">
<div class="mbox">
  <button class="mcl" onclick="closeModal('help-modal')">✕ CLOSE</button>
  <h2>HackRF Sweep — Help Guide</h2>
  <p style="color:var(--dim);font-size:12px">Created by <a href="https://github.com/G4EA5" target="_blank"
     style="color:var(--a2)">G4EA5 @ github.com/G4EA5</a></p>

  <h3>What is this?</h3>
  <p>A real-time radio spectrum analyser. Your HackRF One scans a frequency range and draws a <b>waterfall</b> (time scrolls down, bright = strong signal) and a <b>spectrum graph</b> (power vs frequency).</p>

  <h3>Why 20 MHz chunks?</h3>
  <p>The HackRF hardware processes exactly 20 MHz at a time internally. Spans must be exact multiples of 20 MHz. Non-multiples cause the second hardware chunk to bleed noise into the display. The SPAN selector enforces this automatically.</p>

  <h3>Frequency Controls</h3>
  <div class="hr"><span class="hk">START</span><span class="hd">Start of scan in MHz. Auto-applies 0.8s after typing, or press Enter.</span></div>
  <div class="hr"><span class="hk">SPAN</span><span class="hd">Width of scan — always a multiple of 20 MHz. End = Start + Span.</span></div>
  <div class="hr"><span class="hk">END display</span><span class="hd">Shows the calculated end frequency. Click END to edit it directly — Start will shift to keep the span.</span></div>
  <div class="hr"><span class="hk">▶ SWEEP</span><span class="hd">Restart scan. Clears waterfall and peak hold.</span></div>
  <div class="hr"><span class="hk">Quick Bands</span><span class="hd">Right panel — jump to named bands. Active band highlighted in green.</span></div>

  <h3>Gain Controls</h3>
  <div class="hr"><span class="hk">LNA (0–40 dB)</span><span class="hd">Low Noise Amplifier. Biggest sensitivity control. Steps of 8 dB. Start at 16.</span></div>
  <div class="hr"><span class="hk">VGA (0–62 dB)</span><span class="hd">Variable Gain Amplifier. Fine-tune level. Steps of 2 dB. Start at 20.</span></div>
  <div class="hr"><span class="hk">AMP (ON/OFF)</span><span class="hd">~11 dB broadband pre-amp. Use only for very weak signals. Can overload near strong transmitters.</span></div>

  <h3>Display Controls (left panel)</h3>
  <div class="hr"><span class="hk">MIN dB</span><span class="hd">Noise floor colour. Set just above your noise. Typical: –100 to –85 dBm.</span></div>
  <div class="hr"><span class="hk">MAX dB</span><span class="hd">Peak signal colour. FM stations: –50 to –20 dBm.</span></div>
  <div class="hr"><span class="hk">BIN WIDTH</span><span class="hd">Frequency resolution per sample. 100 kHz ideal for FM.</span></div>
  <div class="hr"><span class="hk">WF SPEED</span><span class="hd">Sweeps combined per waterfall row. Higher = cleaner, slower scroll.</span></div>

  <h3>Modes & Actions</h3>
  <div class="hr"><span class="hk">AVG</span><span class="hd">Smooth spectrum over sweeps. Recommended ON.</span></div>
  <div class="hr"><span class="hk">PEAK</span><span class="hd">Orange line holds max power seen.</span></div>
  <div class="hr"><span class="hk">⚡ AUTO</span><span class="hd">Auto-sets MIN/MAX dB from current scan data.</span></div>
  <div class="hr"><span class="hk">◈ MARK</span><span class="hd">Click waterfall/spectrum to drop a marker. Right-click to remove.</span></div>
  <div class="hr"><span class="hk">↓ PNG</span><span class="hd">Save ruler + spectrum + waterfall as PNG.</span></div>
  <div class="hr"><span class="hk">📻 FM</span><span class="hd">Listen to an FM station via hackrf_transfer + sox.</span></div>
  <div class="hr"><span class="hk">💾 SETTINGS</span><span class="hd">Save/load all settings to browser local storage. Factory defaults.</span></div>

  <h3>Keyboard Shortcuts</h3>
  <div class="hr"><span class="hk"><span class="tag">Space</span></span><span class="hd">Restart sweep</span></div>
  <div class="hr"><span class="hk"><span class="tag">P</span></span><span class="hd">Toggle Peak Hold</span></div>
  <div class="hr"><span class="hk"><span class="tag">A</span></span><span class="hd">Toggle Averaging</span></div>
  <div class="hr"><span class="hk"><span class="tag">M</span></span><span class="hd">Toggle Marker mode</span></div>
  <div class="hr"><span class="hk"><span class="tag">X</span></span><span class="hd">Clear all markers</span></div>
  <div class="hr"><span class="hk"><span class="tag">S</span></span><span class="hd">Save screenshot PNG</span></div>
  <div class="hr"><span class="hk"><span class="tag">+/-</span></span><span class="hd">MAX dB ±5</span></div>
  <div class="hr"><span class="hk"><span class="tag">[/]</span></span><span class="hd">MIN dB ±5</span></div>

  <h3>Troubleshooting</h3>
  <div class="hr"><span class="hk">Blank waterfall</span><span class="hd">Check Console. Run <code>hackrf_info</code>. Check USB.</span></div>
  <div class="hr"><span class="hk">Wrong frequencies</span><span class="hd">Always use exact 20 MHz span multiples. The SPAN dropdown enforces this.</span></div>
  <div class="hr"><span class="hk">All one colour</span><span class="hd">Press ⚡ AUTO or adjust MIN/MAX dB manually.</span></div>
</div></div>

<!-- ═══════════ REQUIREMENTS MODAL ═══════════ -->
<div id="req-modal" class="modal" onclick="modalBackdrop(event,this)">
<div class="mbox">
  <button class="mcl" onclick="closeModal('req-modal')">✕ CLOSE</button>
  <h2>Requirements &amp; Installation</h2>
  <h3>Hardware</h3>
  <p><b>HackRF One</b> or compatible clone. USB 2.0/3.0. Antenna suited to your frequency range.</p>
  <h3>Python Packages</h3>
  <p><code>pip3 install flask flask-sock</code> — Python 3.7+</p>
  <h3>System Tools</h3>
  <p><code>hackrf_sweep</code> — hackrf package &nbsp;|&nbsp;
     <code>lsof</code> — usually pre-installed &nbsp;|&nbsp;
     <code>fuser</code> — psmisc package &nbsp;|&nbsp;
     <code>sox</code> — for FM audio (optional)</p>
  <h3>OS Compatibility</h3>
  <div class="os-grid">
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Ubuntu 18.04+</div><p><code>sudo apt install hackrf lsof psmisc sox</code></p></div>
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Debian 10+</div><p>Same apt command.</p></div>
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Raspberry Pi OS</div><p>Fully supported. Pi 3B+ or better.</p></div>
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Kali Linux</div><p>hackrf usually pre-installed.</p></div>
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Fedora/RHEL 8+</div><p><code>sudo dnf install hackrf sox</code></p></div>
    <div class="os-card"><div class="osn"><span class="gd ok"></span>Arch Linux</div><p><code>sudo pacman -S hackrf sox</code></p></div>
    <div class="os-card"><div class="osn"><span class="gd warn"></span>macOS 12+</div><p><code>brew install hackrf sox</code></p></div>
    <div class="os-card"><div class="osn"><span class="gd warn"></span>Windows WSL2</div><p>usbipd-win for USB passthrough.</p></div>
  </div>
  <h3>Permissions (Linux)</h3>
  <p><code>sudo python3 server.py</code> or add to plugdev:<br>
  <code>sudo usermod -aG plugdev $USER</code><br>
  <code>sudo cp /usr/lib/udev/rules.d/53-hackrf.rules /etc/udev/rules.d/</code><br>
  <code>sudo udevadm control --reload-rules &amp;&amp; sudo udevadm trigger</code><br>
  Then log out and back in.</p>
  <h3>Verify</h3>
  <p><code>hackrf_info</code> should show serial number and firmware.</p>
  <h3>Firmware</h3>
  <p>2018.01.1+ recommended. github.com/greatscottgadgets/hackrf/releases</p>
  <h3>Credits</h3>
  <p>Created by <a href="https://github.com/G4EA5" target="_blank" style="color:var(--a2)">G4EA5</a> — github.com/G4EA5/hackrf_sweep</p>
</div></div>

<script>
// ═══════════════════════════════════════════════════════════
// CANVAS
// ═══════════════════════════════════════════════════════════
const wfCanvas   =document.getElementById('waterfall');
const wfCtx      =wfCanvas.getContext('2d');
const specCanvas =document.getElementById('spectrum-canvas');
const specCtx    =specCanvas.getContext('2d');
const rulerCanvas=document.getElementById('ruler-canvas');
const rulerCtx   =rulerCanvas.getContext('2d');

// ═══════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════
let wfY=0, peakHold=false, useAvg=true, wfSpeed=4;
let peakBuf=null, avgBuf=null, latestBins=null;
let wfAccumBuf=null, wfAccumCount=0;

// THE KEY FIX: freqStart/freqEnd are ONLY updated when the server sends
// a confirmed {"type":"range"} message — never from local UI state.
// This prevents the mapping mismatch caused by stale data arriving
// after a frequency change.
let freqStart=88, freqEnd=108;

let rateCount=0, lastRateTs=Date.now();
let markers=[], markerMode=false;
// sweepActive: false means we discard all incoming CSV lines until
// the server confirms the new range with a "range" message
let sweepActive=false;
let sweepBins=[], sweepFreqLow=null, sweepFreqHigh=null;
let expectedStartHz=null;
let lastChunkHzHigh=null;
let lineBuf='', ws=null;
let gainDebounceTimer=null, freqTimer=null, resizeTimer=null;
let currentQB=null;  // id of active quick-band button
let lastPartialWarn=0;

// ═══════════════════════════════════════════════════════════
// CONSOLE  — throttle DOM updates, max 200 lines
// ═══════════════════════════════════════════════════════════
let consoleCollapsed=false, errCount=0, warnCount=0;
function toggleConsole(){
  consoleCollapsed=!consoleCollapsed;
  document.getElementById('console-wrap').classList.toggle('collapsed',consoleCollapsed);
  document.getElementById('ctoggle').textContent=consoleCollapsed?'▼':'▲';
}
function logMsg(level,msg){
  const el=document.getElementById('console-log');
  const d=document.createElement('div');
  d.className='log-'+level;
  d.textContent=`[${new Date().toTimeString().slice(0,8)}] ${msg}`;
  el.appendChild(d);
  while(el.children.length>200) el.removeChild(el.firstChild);
  el.scrollTop=el.scrollHeight;
  const badge=document.getElementById('cbadge');
  if(level==='error'){
    errCount++;badge.textContent=`${errCount} ERR`;badge.className='has-err';
    if(consoleCollapsed) toggleConsole();
  } else if(level==='warn'&&errCount===0){
    warnCount++;badge.textContent=`${warnCount} WARN`;
    if(!badge.className.includes('has-err')) badge.className='has-warn';
  }
}

// ═══════════════════════════════════════════════════════════
// TOOLTIP SMART POSITIONING
// ═══════════════════════════════════════════════════════════
document.addEventListener('mouseover', e=>{
  const hi = e.target.closest('.hi');
  if(!hi) return;
  const tt = hi.querySelector('.tt');
  if(!tt) return;
  const r = hi.getBoundingClientRect();
  const ttW = 230, ttH = 160, margin = 8;
  let left = r.right + margin;
  let top  = r.top;
  // flip left if would go off right edge
  if(left + ttW > window.innerWidth - margin) left = r.left - ttW - margin;
  // clamp top so it doesn't go off bottom
  if(top + ttH > window.innerHeight - margin) top = window.innerHeight - ttH - margin;
  if(top < margin) top = margin;
  tt.style.left = left + 'px';
  tt.style.top  = top  + 'px';
});

// ═══════════════════════════════════════════════════════════
// SPAN LOGIC — always multiples of 20 MHz
// ═══════════════════════════════════════════════════════════
function getSpanMhz(){
  return parseInt(document.getElementById('span-sel').value)||20;
}
function computeEnd(start, span){
  return Math.min(6000, start + span);
}
function updateEndDisplay(){
  const s=+document.getElementById('start').value||88;
  const span=getSpanMhz();
  const end=computeEnd(s,span);
  document.getElementById('end-display').textContent=end;
  return {s, end, span};
}
function onSpanChange(){
  updateEndDisplay();
  clearTimeout(freqTimer);
  freqTimer=setTimeout(sendSweepConfig, 200);
}

// ═══════════════════════════════════════════════════════════
// COLOUR SCHEMES
// ═══════════════════════════════════════════════════════════
const SCHEMES={
  classic:    [[0,[0,0,0]],[.2,[0,0,160]],[.4,[0,190,230]],[.6,[40,220,40]],[.75,[240,230,0]],[.9,[255,80,0]],[1,[255,255,255]]],
  grayscale:  [[0,[0,0,0]],[1,[255,255,255]]],
  nightvision:[[0,[0,0,0]],[.4,[0,60,0]],[.7,[0,200,0]],[1,[180,255,180]]],
  hot:        [[0,[0,0,0]],[.33,[180,0,0]],[.66,[255,180,0]],[1,[255,255,220]]],
  viridis:    [[0,[68,1,84]],[.25,[59,82,139]],[.5,[33,145,140]],[.75,[94,201,98]],[1,[253,231,37]]],
};
let LUT=buildLUT('classic');
function buildLUT(name){
  const stops=SCHEMES[name]||SCHEMES.classic;
  const lut=new Uint8ClampedArray(512*3);
  for(let i=0;i<512;i++){
    const t=i/511;
    let lo=stops[0],hi=stops[stops.length-1];
    for(let s=0;s<stops.length-1;s++){
      if(t>=stops[s][0]&&t<=stops[s+1][0]){lo=stops[s];hi=stops[s+1];break}
    }
    const f=(t-lo[0])/(hi[0]-lo[0]);
    lut[i*3]  =Math.round(lo[1][0]+f*(hi[1][0]-lo[1][0]));
    lut[i*3+1]=Math.round(lo[1][1]+f*(hi[1][1]-lo[1][1]));
    lut[i*3+2]=Math.round(lo[1][2]+f*(hi[1][2]-lo[1][2]));
  }
  return lut;
}
function setScheme(name){ LUT=buildLUT(name); }
function dbToColor(db,mn,mx,out,off){
  const idx=Math.max(0,Math.min(511,Math.round((db-mn)/(mx-mn)*511)));
  out[off]=LUT[idx*3];out[off+1]=LUT[idx*3+1];out[off+2]=LUT[idx*3+2];out[off+3]=255;
}

// ═══════════════════════════════════════════════════════════
// RESIZE
// ═══════════════════════════════════════════════════════════
function resize(){
  const W=document.getElementById('display').clientWidth||800;
  const wfH=document.getElementById('waterfall-wrap').clientHeight||300;
  wfCanvas.width=W; wfCanvas.height=wfH;
  specCanvas.width=W; specCanvas.height=150;
  rulerCanvas.width=W; rulerCanvas.height=30;
  peakBuf=new Float32Array(W).fill(-999);
  avgBuf =new Float32Array(W).fill(-999);
  wfAccumBuf=new Float32Array(W).fill(-999); wfAccumCount=0;
  wfY=0;
  wfCtx.fillStyle='#000'; wfCtx.fillRect(0,0,W,wfH);
  // reset accumulator only — freqStart/freqEnd stay correct
  resetSweepAssembly();
  drawRuler(); updateDbAxis();
}
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(resize,120)});
setTimeout(resize,80);

// ═══════════════════════════════════════════════════════════
// RULER
// ═══════════════════════════════════════════════════════════
function drawRuler(){
  const W=rulerCanvas.width, H=30, span=freqEnd-freqStart;
  if(span<=0)return;
  rulerCtx.fillStyle='#0c0f16'; rulerCtx.fillRect(0,0,W,H);
  const cands=[0.5,1,2,5,10,20,50,100,200,500,1000];
  const step=cands.find(c=>(span/c)<=16&&(span/c)>=3)||cands[cands.length-1];
  rulerCtx.strokeStyle='#223322'; rulerCtx.fillStyle='#88cc99';
  rulerCtx.font='bold 13px Share Tech Mono'; rulerCtx.textAlign='center';
  for(let f=Math.ceil(freqStart/step)*step; f<=freqEnd+step*0.01;
      f=Math.round((f+step)*10000)/10000){
    if(f>freqEnd+0.001) break;
    const x=Math.round((f-freqStart)/span*W);
    rulerCtx.beginPath(); rulerCtx.moveTo(x,18); rulerCtx.lineTo(x,30); rulerCtx.stroke();
    const lbl=f>=1000?(f/1000).toFixed(f%1000===0?0:1)+'G':f.toFixed(f<10?1:0);
    rulerCtx.fillText(lbl,x,14);
  }
  drawMarkersOnRuler();
}
function updateDbAxis(){
  const mn=+document.getElementById('mindb').value;
  const mx=+document.getElementById('maxdb').value;
  document.getElementById('db-top').textContent=mx;
  document.getElementById('db-mid').textContent=Math.round((mn+mx)/2);
  document.getElementById('db-bot').textContent=mn;
}

// ═══════════════════════════════════════════════════════════
// SIGNAL MONITOR
// ═══════════════════════════════════════════════════════════
function updateSignalMonitor(bins){
  if(!bins||!bins.length) return;
  let maxV=-999, maxI=0;
  for(let i=0;i<bins.length;i++) if(bins[i]>maxV){maxV=bins[i];maxI=i;}
  const peakFreq=freqStart+(maxI/bins.length)*(freqEnd-freqStart);
  document.getElementById('peak-readout').textContent=maxV.toFixed(1)+' dBm';
  document.getElementById('peak-freq').textContent='@ '+peakFreq.toFixed(3)+' MHz';
  const mn=+document.getElementById('mindb').value;
  const mx=+document.getElementById('maxdb').value;
  const pct=Math.max(0,Math.min(100,(maxV-mn)/(mx-mn)*100));
  document.getElementById('sig-bar').style.width=pct+'%';
}

// ═══════════════════════════════════════════════════════════
// SPECTRUM
// ═══════════════════════════════════════════════════════════
function drawSpectrum(bins){
  if(!bins||!bins.length) return;
  const W=specCanvas.width, H=specCanvas.height;
  const mn=+document.getElementById('mindb').value;
  const mx=+document.getElementById('maxdb').value;
  for(let x=0;x<W;x++){
    const si=x/W*bins.length, lo=Math.floor(si), hi=Math.min(lo+1,bins.length-1);
    const v=bins[lo]+(bins[hi]-bins[lo])*(si-lo);
    avgBuf[x]=avgBuf[x]<-990?v:avgBuf[x]*0.72+v*0.28;
    if(peakHold) peakBuf[x]=Math.max(peakBuf[x]<-990?-999:peakBuf[x],v);
  }
  specCtx.fillStyle='#040a04'; specCtx.fillRect(0,0,W,H);
  specCtx.strokeStyle='#142014'; specCtx.lineWidth=1;
  for(let db=Math.ceil(mn/10)*10;db<=mx;db+=10){
    const y=H-(db-mn)/(mx-mn)*H;
    specCtx.beginPath(); specCtx.moveTo(40,y); specCtx.lineTo(W,y); specCtx.stroke();
  }
  const drawLine=(buf,color,fill)=>{
    specCtx.beginPath();
    for(let x=0;x<W;x++){
      const v=buf[x]<-990?mn:buf[x];
      const y=H-Math.max(0,Math.min(1,(v-mn)/(mx-mn)))*H;
      x?specCtx.lineTo(x,y):specCtx.moveTo(x,y);
    }
    if(fill){
      specCtx.lineTo(W,H); specCtx.lineTo(0,H); specCtx.closePath();
      const g=specCtx.createLinearGradient(0,0,0,H);
      g.addColorStop(0,'rgba(0,255,136,.18)'); g.addColorStop(1,'rgba(0,255,136,0)');
      specCtx.fillStyle=g; specCtx.fill();
    }
    specCtx.strokeStyle=color; specCtx.lineWidth=fill?1.5:1; specCtx.stroke();
  };
  const src=useAvg?avgBuf:(()=>{
    const b=new Float32Array(W);
    for(let x=0;x<W;x++){
      const si=x/W*bins.length,lo=Math.floor(si),hi=Math.min(lo+1,bins.length-1);
      b[x]=bins[lo]+(bins[hi]-bins[lo])*(si-lo);
    }
    return b;
  })();
  drawLine(src,'#00ff88',true);
  if(peakHold) drawLine(peakBuf,'#ff6622',false);
  drawMarkersOnSpectrum();
}

// ═══════════════════════════════════════════════════════════
// WATERFALL — accumulate wfSpeed sweeps then draw one row
// ═══════════════════════════════════════════════════════════
function drawWaterfallLine(bins){
  if(!bins||!bins.length) return;
  const W=wfCanvas.width;
  if(!wfAccumBuf||wfAccumBuf.length!==W){
    wfAccumBuf=new Float32Array(W).fill(-999); wfAccumCount=0;
  }
  for(let x=0;x<W;x++){
    const si=x/W*bins.length, lo=Math.floor(si), hi=Math.min(lo+1,bins.length-1);
    const v=bins[lo]+(bins[hi]-bins[lo])*(si-lo);
    wfAccumBuf[x]=wfAccumBuf[x]<-990?v:Math.max(wfAccumBuf[x],v);
  }
  wfAccumCount++;
  if(wfAccumCount<wfSpeed) return;
  const mn=+document.getElementById('mindb').value;
  const mx=+document.getElementById('maxdb').value;
  const img=wfCtx.createImageData(W,1);
  for(let x=0;x<W;x++)
    dbToColor(wfAccumBuf[x]<-990?mn:wfAccumBuf[x],mn,mx,img.data,x*4);
  wfCtx.putImageData(img,0,wfY);
  wfY=(wfY+1)%wfCanvas.height;
  wfAccumBuf.fill(-999); wfAccumCount=0;
}

// ═══════════════════════════════════════════════════════════
// SWEEP ASSEMBLY — reject partial/corrupt passes (backlog drops)
// ═══════════════════════════════════════════════════════════
function expectedSweepBins(){
  const spanMhz=Math.max(1, freqEnd-freqStart);
  const bw=Math.max(50000, +document.getElementById('binwidth').value||100000);
  return Math.max(8, Math.round(spanMhz*1e6/bw));
}

function sweepLooksComplete(){
  // Legacy threshold — strict checks left the waterfall blank when lines were
  // decimated over LAN (remote viewer cannot keep up with 400 raw lines/s).
  return sweepBins.length >= 4;
}

function resetSweepAssembly(){
  sweepBins=[]; sweepFreqLow=null; sweepFreqHigh=null;
  expectedStartHz=null; lastChunkHzHigh=null;
}

// ═══════════════════════════════════════════════════════════
// WATERFALL RESET — wipes canvas, resets all accumulators
// Does NOT change freqStart/freqEnd — those come from server
// ═══════════════════════════════════════════════════════════
function resetWaterfall(){
  wfY=0;
  wfCtx.fillStyle='#000'; wfCtx.fillRect(0,0,wfCanvas.width,wfCanvas.height);
  peakBuf=new Float32Array(wfCanvas.width).fill(-999);
  avgBuf =new Float32Array(wfCanvas.width).fill(-999);
  if(wfAccumBuf) wfAccumBuf.fill(-999); wfAccumCount=0;
  resetSweepAssembly();
  // Mark as inactive — will reactivate when server sends "range" message
  sweepActive=false;
}

// ═══════════════════════════════════════════════════════════
// MARKERS
// ═══════════════════════════════════════════════════════════
function toggleMarkerMode(){
  markerMode=!markerMode;
  const lbl=`◈ MARKERS: ${markerMode?'ON':'OFF'}`;
  document.getElementById('marker-mode-btn').textContent=lbl;
  document.getElementById('marker-mode-btn').className='abtn'+(markerMode?' act':'');
  document.getElementById('mrk-btn').textContent=lbl.replace('◈ ','◈ ');
  document.getElementById('mrk-btn').className='pbtn'+(markerMode?' act':'');
}
function clearMarkers(){
  markers=[]; updateMarkerList(); drawRuler();
  if(latestBins) drawSpectrum(latestBins);
}
function addMarker(freq){
  if(markers.some(m=>Math.abs(m.freq-freq)<0.05)) return;
  markers.push({freq, label:freq.toFixed(3)+' MHz'});
  updateMarkerList(); drawRuler(); if(latestBins) drawSpectrum(latestBins);
}
function updateMarkerList(){
  const el=document.getElementById('marker-list');
  if(!markers.length){el.textContent='No markers';return;}
  el.innerHTML=markers.map((m,i)=>
    `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--b1)">
      <span style="color:var(--a2);font-size:12px">${m.label}</span>
      <span style="cursor:pointer;color:var(--danger);font-size:14px;padding:0 4px"
            onclick="removeMarker(${i})">✕</span>
    </div>`
  ).join('');
}
function removeMarker(i){
  markers.splice(i,1); updateMarkerList(); drawRuler();
  if(latestBins) drawSpectrum(latestBins);
}
function freqToX(freq,W){
  return Math.round((freq-freqStart)/(freqEnd-freqStart)*W);
}
function drawMarkersOnRuler(){
  const W=rulerCanvas.width;
  markers.forEach(m=>{
    const x=freqToX(m.freq,W); if(x<0||x>W) return;
    rulerCtx.strokeStyle='#ff4455'; rulerCtx.lineWidth=1.5;
    rulerCtx.beginPath(); rulerCtx.moveTo(x,0); rulerCtx.lineTo(x,30); rulerCtx.stroke();
    rulerCtx.fillStyle='#ff6677'; rulerCtx.font='10px Share Tech Mono';
    rulerCtx.textAlign='center'; rulerCtx.fillText('▼',x,28);
  });
}
function drawMarkersOnSpectrum(){
  const W=specCanvas.width, H=specCanvas.height;
  specCtx.setLineDash([3,3]);
  markers.forEach(m=>{
    const x=freqToX(m.freq,W); if(x<0||x>W) return;
    specCtx.strokeStyle='rgba(255,68,85,.7)'; specCtx.lineWidth=1;
    specCtx.beginPath(); specCtx.moveTo(x,0); specCtx.lineTo(x,H); specCtx.stroke();
    specCtx.fillStyle='#ff6677'; specCtx.font='10px Share Tech Mono';
    specCtx.textAlign='center'; specCtx.fillText(m.label,x,H-4);
  });
  specCtx.setLineDash([]);
}
wfCanvas.addEventListener('contextmenu',e=>{
  e.preventDefault();
  const r=wfCanvas.getBoundingClientRect();
  const freq=freqStart+(e.clientX-r.left)/wfCanvas.width*(freqEnd-freqStart);
  const nb=markers.reduce((b,m)=>{const d=Math.abs(m.freq-freq);return d<b.d?{d,m}:b},{d:Infinity,m:null});
  if(nb.m&&nb.d<(freqEnd-freqStart)*0.01){
    markers=markers.filter(m=>m!==nb.m);
    updateMarkerList(); drawRuler(); if(latestBins) drawSpectrum(latestBins);
  }
});

// ═══════════════════════════════════════════════════════════
// AUTO SCALE
// ═══════════════════════════════════════════════════════════
function autoScale(){
  if(!latestBins||latestBins.length<4){
    logMsg('warn','Auto Scale: wait for at least one sweep');return;
  }
  const sorted=[...latestBins].filter(v=>isFinite(v)).sort((a,b)=>a-b);
  const n=sorted.length;
  const newMin=Math.max(-120,Math.min(-40,Math.floor(sorted[Math.floor(n*0.10)]/5)*5-5));
  const newMax=Math.max(-80, Math.min(0,  Math.ceil( sorted[Math.floor(n*0.99)]/5)*5+5));
  if(newMin>=newMax){logMsg('warn','Auto Scale: range too narrow');return;}
  document.getElementById('mindb').value=newMin;
  document.getElementById('maxdb').value=newMax;
  document.getElementById('mindb-val').textContent=(newMin<0?'–':'')+Math.abs(newMin);
  document.getElementById('maxdb-val').textContent=(newMax<0?'–':'')+Math.abs(newMax);
  updateDbAxis();
  logMsg('info',`Auto Scale: ${newMin} to ${newMax} dBm`);
}

// ═══════════════════════════════════════════════════════════
// EXPORT PNG
// ═══════════════════════════════════════════════════════════
function exportPNG(){
  const totalH=rulerCanvas.height+specCanvas.height+wfCanvas.height;
  const W=wfCanvas.width;
  const tmp=document.createElement('canvas'); tmp.width=W; tmp.height=totalH;
  const tc=tmp.getContext('2d');
  tc.fillStyle='#07090e'; tc.fillRect(0,0,W,totalH);
  tc.drawImage(rulerCanvas,0,0);
  tc.drawImage(specCanvas,0,rulerCanvas.height);
  tc.drawImage(wfCanvas,0,rulerCanvas.height+specCanvas.height);
  tc.fillStyle='rgba(0,0,0,.6)'; tc.fillRect(0,totalH-22,400,22);
  tc.fillStyle='#00ff88'; tc.font='12px Share Tech Mono';
  tc.fillText(`HackRF ${freqStart}–${freqEnd} MHz  ${new Date().toLocaleString()}  github.com/G4EA5/hackrf_sweep`,
              6, totalH-6);
  const a=document.createElement('a');
  a.download=`hackrf_${freqStart}-${freqEnd}MHz_${Date.now()}.png`;
  a.href=tmp.toDataURL('image/png'); a.click();
  logMsg('info',`Screenshot: ${a.download}`);
}

// ═══════════════════════════════════════════════════════════
// QUICK BANDS
// ═══════════════════════════════════════════════════════════
// bands: {id, start, span}
const BAND_MAP={
  'qb-fm'    :{start:87,  span:20},
  'qb-dab'   :{start:174, span:20},
  'qb-air'   :{start:118, span:20},
  'qb-adsb'  :{start:1080,span:20},
  'qb-marine':{start:156, span:20},
  'qb-2m'    :{start:144, span:20},
  'qb-70cm'  :{start:430, span:20},
  'qb-pmr'   :{start:446, span:20},
  'qb-gsm'   :{start:860, span:40},
  'qb-lte'   :{start:1800,span:40},
  'qb-gps'   :{start:1560,span:20},
  'qb-noaa'  :{start:136, span:20},
  'qb-wifi24':{start:2400,span:100},
  'qb-wifi5' :{start:5160,span:500},
  'qb-irid'  :{start:1616,span:20},
  'qb-full'  :{start:1,   span:5980},
};

function quickBand(id, start, span){
  // highlight active band
  if(currentQB){
    const prev=document.getElementById(currentQB);
    if(prev) prev.className='pbtn';
  }
  currentQB=id;
  const btn=document.getElementById(id);
  if(btn) btn.className='pbtn qact';

  // set controls
  document.getElementById('start').value=start;
  // find smallest span option >= requested span
  const spanOpts=Array.from(document.getElementById('span-sel').options).map(o=>+o.value);
  const best=spanOpts.find(o=>o>=span)||spanOpts[spanOpts.length-1];
  document.getElementById('span-sel').value=best;
  updateEndDisplay();

  // send immediately (no debounce needed — user explicitly clicked)
  sendSweepConfig();
}

function highlightCurrentBand(){
  // after a freq change, check if start/span matches any known band and highlight it
  const s=+document.getElementById('start').value;
  const span=getSpanMhz();
  let found=null;
  for(const [id,b] of Object.entries(BAND_MAP)){
    if(b.start===s&&b.span===span){found=id;break;}
  }
  // clear all
  Object.keys(BAND_MAP).forEach(id=>{
    const el=document.getElementById(id);
    if(el) el.className='pbtn'+(id===found?' qact':'');
  });
  currentQB=found;
}

// ═══════════════════════════════════════════════════════════
// FM LISTEN (stub — server-side hackrf_transfer needed)
// ═══════════════════════════════════════════════════════════
let fmProc=false;
function startFM(){
  const freq=+document.getElementById('fm-tune-freq').value;
  if(freq<87.5||freq>108){logMsg('warn','FM: frequency must be 87.5–108 MHz');return;}
  document.getElementById('fm-modal-status').textContent=
    `Sending tune request for ${freq} MHz…`;
  if(ws&&ws.readyState===1){
    ws.send(JSON.stringify({cmd:'fmListen',freq:Math.round(freq*1e6)}));
  }
  document.getElementById('fm-bar').className='fm-bar visible';
  document.getElementById('fm-freq-display').textContent=freq.toFixed(1)+' MHz';
  document.getElementById('fm-status').textContent='Listening (requires sox on server)';
  fmProc=true;
}
function stopFM(){
  if(ws&&ws.readyState===1) ws.send(JSON.stringify({cmd:'fmStop'}));
  document.getElementById('fm-bar').className='fm-bar';
  document.getElementById('fm-modal-status').textContent='Stopped';
  fmProc=false;
}
function recordFM(){
  const freq=+document.getElementById('fm-tune-freq').value;
  document.getElementById('fm-modal-status').textContent=
    `Recording ${freq} MHz — check server terminal for output file`;
  if(ws&&ws.readyState===1)
    ws.send(JSON.stringify({cmd:'fmRecord',freq:Math.round(freq*1e6)}));
}

// ═══════════════════════════════════════════════════════════
// SETTINGS (localStorage)
// ═══════════════════════════════════════════════════════════
const DEFAULTS={
  start:88, span:20, lna:16, vga:20, amp:0,
  mindb:-100, maxdb:-20, binwidth:100000, wfspeed:4, scheme:'classic'
};
function getSettingsObj(){
  return {
    start:  +document.getElementById('start').value,
    span:   getSpanMhz(),
    lna:    +document.getElementById('lna').value,
    vga:    +document.getElementById('vga').value,
    amp:    +document.getElementById('amp').value,
    mindb:  +document.getElementById('mindb').value,
    maxdb:  +document.getElementById('maxdb').value,
    binwidth:+document.getElementById('binwidth').value,
    wfspeed:+document.getElementById('wfspeed').value,
    scheme: document.getElementById('scheme-select').value,
  };
}
function applySettingsObj(s){
  document.getElementById('start').value=s.start;
  document.getElementById('span-sel').value=s.span;
  document.getElementById('lna').value=s.lna;
  document.getElementById('vga').value=s.vga;
  document.getElementById('amp').value=s.amp;
  document.getElementById('mindb').value=s.mindb;
  document.getElementById('maxdb').value=s.maxdb;
  document.getElementById('binwidth').value=s.binwidth;
  document.getElementById('wfspeed').value=s.wfspeed;
  document.getElementById('scheme-select').value=s.scheme;
  // update displayed values
  document.getElementById('lna-val').textContent=s.lna+'dB';
  document.getElementById('vga-val').textContent=s.vga+'dB';
  document.getElementById('amp-val').textContent=s.amp?'ON':'OFF';
  document.getElementById('mindb-val').textContent=(s.mindb<0?'–':'')+Math.abs(s.mindb);
  document.getElementById('maxdb-val').textContent=(s.maxdb<0?'–':'')+Math.abs(s.maxdb);
  document.getElementById('bw-val').textContent=(s.binwidth/1000|0)+'k';
  document.getElementById('wfspeed-val').textContent=s.wfspeed;
  wfSpeed=s.wfspeed;
  setScheme(s.scheme);
  updateEndDisplay(); updateDbAxis();
}
function saveSettings(){
  const s=getSettingsObj();
  localStorage.setItem('hackrf_settings',JSON.stringify(s));
  document.getElementById('settings-status').textContent='✓ Saved to browser storage';
  showSettingsPreview(s);
}
function loadSettings(){
  const raw=localStorage.getItem('hackrf_settings');
  if(!raw){document.getElementById('settings-status').textContent='No saved settings found';return;}
  try{
    const s=JSON.parse(raw);
    applySettingsObj(s);
    document.getElementById('settings-status').textContent='✓ Settings loaded';
    showSettingsPreview(s);
    sendSweepConfig();
  }catch(e){
    document.getElementById('settings-status').textContent='Error loading settings';
  }
}
function factoryDefaults(){
  applySettingsObj(DEFAULTS);
  document.getElementById('settings-status').textContent='↺ Factory defaults applied';
  sendSweepConfig();
}
function showSettingsPreview(s){
  document.getElementById('settings-preview').innerHTML=
    Object.entries(s).map(([k,v])=>
      `<div style="display:flex;gap:8px"><span style="color:var(--dim);min-width:90px">${k}</span>
       <span style="color:var(--a2)">${v}</span></div>`
    ).join('');
}
document.getElementById('settings-modal').addEventListener('click',e=>{
  if(e.target.closest('.mbox')) showSettingsPreview(getSettingsObj());
});

// Try to auto-load saved settings on startup
(function(){
  const raw=localStorage.getItem('hackrf_settings');
  if(raw){
    try{applySettingsObj(JSON.parse(raw));}catch(e){}
  }
})();

// ═══════════════════════════════════════════════════════════
// WEBSOCKET
// ═══════════════════════════════════════════════════════════
function connect(){
  ws=new WebSocket('ws://'+location.host+'/ws');
  ws.onopen=()=>{
    document.getElementById('sdot').className='ok';
    document.getElementById('stxt').textContent='connected';
    logMsg('info','WebSocket connected');
    sendSweepConfig();  // sync saved UI settings before/at sweep start
  };
  ws.onclose=()=>{
    document.getElementById('sdot').className='err';
    document.getElementById('stxt').textContent='reconnecting…';
    logMsg('warn','WebSocket closed — reconnecting in 2s');
    setTimeout(connect,2000);
  };
  ws.onerror=()=>logMsg('error','WebSocket error — check server is running');

  lineBuf=''; resetSweepAssembly();
  sweepActive=false; initialSyncDone=false;

  ws.onmessage=(ev)=>{
    const raw=ev.data;

    // ── JSON messages from server ───────────────────────
    if(raw.charCodeAt(0)===123){ // '{'
      try{
        const d=JSON.parse(raw);
        if(d.type==='status'){
          logMsg(d.level||'info',d.msg);
          if(d.msg.includes('hackrf: Found HackRF')||d.msg.includes('hackrf: Serial'))
            document.getElementById('device-info').textContent=
              d.msg.replace('hackrf: ','').trim();
          return;
        }
        if(d.type==='range'){
          // Server has killed the old process, drained the queue,
          // and started the new process. Only NOW do we update our
          // frequency mapping and re-enable data acceptance.
          freqStart=d.start; freqEnd=d.end;
          document.getElementById('end-display').textContent=d.end;
          document.title=`HackRF ${d.start}–${d.end} MHz`;
          const soft=!!d.soft;
          if(soft){
            resetSweepAssembly();
            sweepActive=true;
          } else {
            resetWaterfall();
            sweepActive=true;
            initialSyncDone=true;
          }
          drawRuler(); updateDbAxis();
          highlightCurrentBand();
          return;
        }
      }catch(e){}
      return;
    }

    // ── CSV sweep data ─────────────────────────────────
    // DISCARD if server hasn't confirmed the range yet
    if(!sweepActive) return;

    lineBuf+=raw;
    const lines=lineBuf.split('\n');
    lineBuf=lines.pop();

    for(const line of lines){
      const t=line.trim(); if(!t) continue;
      const parts=t.split(','); if(parts.length<7) continue;
      const hzLow =parseFloat(parts[2]);
      const hzHigh=parseFloat(parts[3]);
      const bins  =parts.slice(6).map(Number).filter(v=>isFinite(v));
      if(!bins.length||!isFinite(hzLow)||!isFinite(hzHigh)) continue;

      // learn start of sweep from first chunk
      if(expectedStartHz===null) expectedStartHz=hzLow;

      // detect new sweep pass
      const isNewSweep=sweepFreqLow!==null && hzLow<=expectedStartHz+5e4;
        if(isNewSweep){
        if(sweepBins.length>=4){
          latestBins=sweepBins.slice();
          drawSpectrum(latestBins);
          drawWaterfallLine(latestBins);
          updateSignalMonitor(latestBins);
          document.getElementById('info-bins').textContent=latestBins.length;
          document.getElementById('info-span').textContent=(freqEnd-freqStart).toFixed(0);
          rateCount++;
          const now=Date.now();
          if(now-lastRateTs>=1000){
            document.getElementById('info-rate').textContent=rateCount;
            rateCount=0; lastRateTs=now;
          }
        }
        resetSweepAssembly();
        expectedStartHz=hzLow;
        sweepFreqLow=hzLow; sweepFreqHigh=hzHigh;
        lastChunkHzHigh=hzHigh;
      } else {
        if(sweepFreqLow===null)  sweepFreqLow=hzLow;
        if(sweepFreqHigh===null) sweepFreqHigh=hzHigh;
        else {
          if(lastChunkHzHigh!==null && hzLow > lastChunkHzHigh + 2e6){
            resetSweepAssembly();
            expectedStartHz=hzLow;
          }
          sweepFreqHigh=Math.max(sweepFreqHigh,hzHigh);
        }
        lastChunkHzHigh=hzHigh;
      }

      // ── Clip bins to confirmed frequency range ──────────────
      // freqStart/freqEnd are now guaranteed correct because we
      // only set them when the server confirmed the range.
      const reqLow =freqStart*1e6;
      const reqHigh=freqEnd  *1e6;
      const binHz  =(hzHigh-hzLow)/bins.length;
      for(let i=0;i<bins.length;i++){
        const bf=hzLow+(i+0.5)*binHz;
        if(bf>=reqLow&&bf<=reqHigh) sweepBins.push(bins[i]);
      }
    }
  };
}

// ═══════════════════════════════════════════════════════════
// CONTROLS
// ═══════════════════════════════════════════════════════════
let initialSyncDone=false;

function sendSweepConfig(){
  if(!ws||ws.readyState!==1){logMsg('warn','Not connected');return;}
  if(initialSyncDone) sweepActive=false;
  const s   =Math.max(1,Math.min(5980,+document.getElementById('start').value||88));
  const span=getSpanMhz();
  const e   =Math.min(6000,s+span);
  document.getElementById('start').value=s;
  // NOTE: we do NOT update freqStart/freqEnd here.
  // They will be updated by the server's "range" confirmation message.
  // resetWaterfall is called by the range handler, not here.
  // This means the old waterfall stays visible until the server is ready.
  updateEndDisplay();
  ws.send(JSON.stringify({
    cmd:'setSweep', start:s, end:e,
    lna:     +document.getElementById('lna').value,
    vga:     +document.getElementById('vga').value,
    amp:     +document.getElementById('amp').value,
    binwidth:+document.getElementById('binwidth').value,
  }));
}
function setSweep(){sendSweepConfig();}

function onGainSlider(id,v,unit){
  document.getElementById(id+'-val').textContent=v+(unit||'');
  clearTimeout(gainDebounceTimer); gainDebounceTimer=setTimeout(sendSweepConfig,900);
}
function onAmpSlider(v){
  document.getElementById('amp-val').textContent=(+v===1)?'ON':'OFF';
  clearTimeout(gainDebounceTimer); gainDebounceTimer=setTimeout(sendSweepConfig,900);
}
function onDisplaySlider(id,v){
  document.getElementById(id+'-val').textContent=(+v<0?'–':'')+Math.abs(+v);
  updateDbAxis();
}
function onBinwidthSlider(v){
  document.getElementById('bw-val').textContent=(+v/1000|0)+'k';
  clearTimeout(gainDebounceTimer); gainDebounceTimer=setTimeout(sendSweepConfig,900);
}
function togglePeak(){
  peakHold=!peakHold;
  document.getElementById('peak-btn').className='mbtn'+(peakHold?' on':'');
  if(!peakHold&&peakBuf) peakBuf.fill(-999);
}
function toggleAvg(){
  useAvg=!useAvg;
  document.getElementById('avg-btn').className='mbtn'+(useAvg?' on':'');
}

// start input — debounce, show pending
document.getElementById('start').addEventListener('input',function(){
  this.classList.add('pending');
  clearTimeout(freqTimer);
  updateEndDisplay();
  freqTimer=setTimeout(()=>{this.classList.remove('pending');sendSweepConfig();},800);
});
document.getElementById('start').addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    clearTimeout(freqTimer);
    e.target.classList.remove('pending');
    sendSweepConfig();
  }
});

// ═══════════════════════════════════════════════════════════
// MOUSE INTERACTIONS
// ═══════════════════════════════════════════════════════════
function freqFromX(clientX,canvas){
  return freqStart+(clientX-canvas.getBoundingClientRect().left)/canvas.width*(freqEnd-freqStart);
}
function onMouseMove(e,canvas){
  const freq=freqFromX(e.clientX,canvas);
  document.getElementById('cursor-freq').textContent=freq.toFixed(3);
  if(latestBins){
    const frac=(e.clientX-canvas.getBoundingClientRect().left)/canvas.width;
    const idx=Math.max(0,Math.min(latestBins.length-1,Math.floor(frac*latestBins.length)));
    document.getElementById('cursor-power').textContent=latestBins[idx].toFixed(1);
  }
  const mt=document.getElementById('marker-tip');
  const near=markers.find(m=>Math.abs(m.freq-freq)<(freqEnd-freqStart)*0.008);
  if(near){
    const r=canvas.getBoundingClientRect();
    mt.style.display='block';
    mt.style.left=(e.clientX-r.left+10)+'px';
    mt.style.top =(e.clientY-r.top -32)+'px';
    mt.textContent=near.label;
  } else mt.style.display='none';
}
function onCanvasClick(e,canvas){
  if(!markerMode) return;
  e.preventDefault();
  addMarker(freqFromX(e.clientX,canvas));
}
document.getElementById('waterfall-wrap').addEventListener('mousemove',e=>onMouseMove(e,wfCanvas));
document.getElementById('spectrum-wrap').addEventListener('mousemove', e=>onMouseMove(e,specCanvas));
wfCanvas.addEventListener('click',  e=>onCanvasClick(e,wfCanvas));
specCanvas.addEventListener('click',e=>onCanvasClick(e,specCanvas));

// ═══════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ═══════════════════════════════════════════════════════════
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||
     e.target.tagName==='TEXTAREA') return;
  if(document.querySelector('.modal.open')) return;
  switch(e.key){
    case ' ':e.preventDefault();sendSweepConfig();break;
    case 'p':case 'P':togglePeak();break;
    case 'a':case 'A':toggleAvg();break;
    case 'm':case 'M':toggleMarkerMode();break;
    case 'x':case 'X':clearMarkers();break;
    case 's':case 'S':exportPNG();break;
    case '+':case '=':{const el=document.getElementById('maxdb');el.value=Math.min(0,+el.value+5);onDisplaySlider('maxdb',el.value);break;}
    case '-':{const el=document.getElementById('maxdb');el.value=Math.max(-80,+el.value-5);onDisplaySlider('maxdb',el.value);break;}
    case '[':{const el=document.getElementById('mindb');el.value=Math.max(-120,+el.value-5);onDisplaySlider('mindb',el.value);break;}
    case ']':{const el=document.getElementById('mindb');el.value=Math.min(-40,+el.value+5);onDisplaySlider('mindb',el.value);break;}
  }
});

// ═══════════════════════════════════════════════════════════
// MODAL HELPERS
// ═══════════════════════════════════════════════════════════
function openModal(id){document.getElementById(id).classList.add('open');}
function closeModal(id){document.getElementById(id).classList.remove('open');}
function modalBackdrop(e,el){if(e.target===el)el.classList.remove('open');}

// ═══════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════
updateEndDisplay();
updateDbAxis();
connect();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print(f"[server] PID {MY_PID}")
    free_port(8085)
    kill_hackrf_users()
    app.run(host="0.0.0.0", port=8085, threaded=True)
