import ttkbootstrap as tb
from ttkbootstrap.constants import *
import tkinter as tk
from tkinter import ttk, messagebox
import threading, subprocess, socket, time, os, sys

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.ticker import MultipleLocator

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np

SERVER_HOST = '127.0.0.1'
SERVER_PORT = 54321
C_EXECUTABLE = "runway_manager.exe"
NUM_RUNWAYS = 3
NUM_PLANES = 10

plane_widgets = {}
plane_table_entries = {}
gantt_data = []


class AirportApp(tb.Window):
    def __init__(self):
        super().__init__(title="Airport Runway Scheduler – User Priority", themename="darkly")
        self.geometry("1150x850")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.c_process = None
        self.sock = None
        self.stop_event = threading.Event()
        self.sim_start_time = time.time()
        self.priority_vars = {}
        self.socket_connected = False

        self.animation_job_id = None
        self.gantt_fig = None
        self.gantt_ax = None

        self.build_ui()
        self.launch_backend()
        threading.Thread(target=self.listen_backend, daemon=True).start()

    def build_ui(self):
        header = ttk.Frame(self, padding=10)
        header.pack(fill='x')
        ttk.Label(header, text="✈️ User-Defined Priority Queue Scheduler",
                  font=("Segoe UI", 18, "bold")).pack(side='left', padx=10)

        priority_frame = ttk.Frame(self, padding=5)
        priority_frame.pack(fill='x', padx=10, pady=(5, 5))
        ttk.Label(priority_frame,
                  text="Set Plane Priorities (1=Highest, 1 to 10):",
                  font=("Segoe UI", 10, "bold")).pack(side='left', padx=(0, 10))

        for i in range(1, NUM_PLANES + 1):
            var = tk.StringVar(value=str(i))
            self.priority_vars[i] = var
            p_group = ttk.Frame(priority_frame)
            p_group.pack(side='left', padx=5)
            ttk.Label(p_group, text=f"P{i}:").pack(side='left')
            tb.Entry(p_group, textvariable=var, width=3, justify='center').pack(side='left')

        self.start_btn = ttk.Button(priority_frame, text="Start Simulation",
                                    command=self.start_simulation, bootstyle="success")
        self.start_btn.pack(side='left', padx=(20, 0))

        self.canvas = tk.Canvas(self, bg="#1a202c", height=250)
        self.canvas.pack(fill='x', padx=10, pady=(5, 10))
        self._draw_runways()

        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(fill='both', expand=True, padx=10, pady=(0, 5))
        bottom_frame.grid_columnconfigure(0, weight=1)
        bottom_frame.grid_columnconfigure(1, weight=1)
        bottom_frame.grid_rowconfigure(0, weight=1)

        table_container = ttk.Frame(bottom_frame)
        table_container.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        table_notebook = ttk.Notebook(table_container)
        table_notebook.pack(fill='both', expand=True)
        status_tab = ttk.Frame(table_notebook)
        table_notebook.add(status_tab, text="✈️ Plane Status")
        cols = ("Plane", "Prio", "Runway", "Status", "Elapsed")
        self.tree = ttk.Treeview(status_tab, columns=cols, show="headings", height=15)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor='center', minwidth=40, width=60)
        self.tree.pack(fill='both', expand=True)

        chart_container = ttk.Frame(bottom_frame)
        chart_container.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        chart_notebook = ttk.Notebook(chart_container)
        chart_notebook.pack(fill='both', expand=True)
        gantt_tab = ttk.Frame(chart_notebook)
        chart_notebook.add(gantt_tab, text="📊 Runway Timeline (Gantt Chart)")
        self.gantt_frame = ttk.Frame(gantt_tab, padding=0)
        self.gantt_frame.pack(fill='both', expand=True)
        plt.style.use('dark_background')
        self.gantt_fig, self.gantt_ax = plt.subplots(figsize=(10, 3.0))
        self.gantt_canvas = FigureCanvasTkAgg(self.gantt_fig, master=self.gantt_frame)
        self.gantt_canvas_widget = self.gantt_canvas.get_tk_widget()
        self.gantt_canvas_widget.pack(fill='both', expand=True, pady=(5, 5))
        self._initialize_gantt()

        self.log = tk.Text(self, height=4, bg="#111", fg="#00ffcc")
        self.log.pack(fill='x', padx=10, pady=(0, 10))

    def _initialize_gantt(self):
        self.gantt_ax.clear()
        self.gantt_ax.set_facecolor('#1a202c')
        self.gantt_ax.set_yticks([10 * (i + 1) for i in range(NUM_RUNWAYS)])
        self.gantt_ax.set_yticklabels([f"R{i + 1}" for i in range(NUM_RUNWAYS)])
        self.gantt_ax.xaxis.set_major_locator(MultipleLocator(2))
        self.gantt_ax.set_xlabel("Time (seconds)")
        self.gantt_ax.grid(axis='x', linestyle='--', alpha=0.5)
        self.gantt_ax.set_xlim(0, 30)
        self.gantt_ax.set_ylim(5, 10 * (NUM_RUNWAYS + 1) - 5)
        self.gantt_fig.tight_layout(pad=0.5)
        self.gantt_canvas.draw()

    def _update_gantt_chart(self):
        global gantt_data
        if not hasattr(self, 'gantt_ax'):
            return
        self.gantt_ax.clear()
        self._initialize_gantt()
        if not gantt_data:
            self.gantt_canvas.draw()
            return
        max_time = max(item['end'] for item in gantt_data)
        self.gantt_ax.set_xlim(0, max_time * 1.1 + 5)
        colors = plt.colormaps.get_cmap('hsv')
        for item in gantt_data:
            runway_idx = item['runway'] - 1
            y_pos = 10 * (runway_idx + 1)
            self.gantt_ax.broken_barh([(item['start'], item['duration'])],
                                      (y_pos - 4, 8),
                                      facecolors=colors(item['plane'] / NUM_PLANES),
                                      edgecolor='black')
            self.gantt_ax.text(item['start'] + item['duration'] / 2, y_pos,
                               f"P{item['plane']}", ha='center', va='center',
                               color='black', fontsize=8, fontweight='bold')
        self.gantt_fig.canvas.draw_idle()

    def _draw_runways(self):
        h = 250
        spacing = (h - 30) / NUM_RUNWAYS
        self.runway_coords = []
        for i in range(NUM_RUNWAYS):
            y1 = 20 + i * spacing
            y2 = y1 + 40
            self.canvas.create_rectangle(80, y1, 1000, y2, fill="#2a384f", outline="#4299e1", width=2)
            for j in range(90, 1000, 30):
                self.canvas.create_line(j, y1 + 20, j + 15, y1 + 20, fill="#fefcbf", width=2)
            self.canvas.create_text(90, y1 + 20, text=f"Runway {i + 1}", anchor='w',
                                    fill="#90cdf4", font=("Segoe UI", 10, "bold"))
            self.runway_coords.append((80, y1, 1000, y2))
            self.canvas.create_line(980, y1, 980, y2, fill="#f56565", width=2)

    def send_command(self, msg):
        if not self.socket_connected or not self.sock:
            self.log.insert("end", "[ERROR] Socket not connected. Cannot send command.\n")
            self.log.see("end")
            return False
        try:
            self.sock.sendall(msg.encode('utf-8'))
            self.log.insert("end", f"[IPC] Sent: {msg.strip()}\n")
            self.log.see("end")
            return True
        except Exception as e:
            self.log.insert("end", f"[ERROR] Failed to send command: {e}\n")
            self.log.see("end")
            return False

    def start_simulation(self):
        if not self.socket_connected:
            messagebox.showerror("Connection Error",
                                 "Backend not connected yet. Please wait a moment and try again.")
            return

        self.tree.delete(*self.tree.get_children())
        global plane_widgets, plane_table_entries, gantt_data
        plane_widgets.clear()
        plane_table_entries.clear()
        gantt_data.clear()
        self.sim_start_time = time.time()
        self._update_gantt_chart()
        self.log.delete(1.0, tk.END)
        self.canvas.delete("all")
        self._draw_runways()

        priorities = []
        try:
            for i in range(1, NUM_PLANES + 1):
                p = int(self.priority_vars[i].get())
                if p <= 0: raise ValueError
                priorities.append(p)
            if len(set(priorities)) != NUM_PLANES:
                raise ValueError
        except ValueError:
            messagebox.showerror("Input Error",
                                 "All plane priorities must be unique positive integers (1-10).")
            return

        for i in range(NUM_PLANES):
            pid = i + 1
            prio = priorities[i]
            item = self.tree.insert('', 'end', values=(pid, prio, 0, "QUEUED", "0s"))
            plane_table_entries[pid] = item

        if self.animation_job_id is None:
            self.animate_planes()

        config_msg = f"CONFIG,{NUM_RUNWAYS},{NUM_PLANES},{','.join(map(str, priorities))}\r\n"
        self.log.insert("end", f"[DEBUG] Sending config: {config_msg.strip()}\n")
        self.log.see("end")
        
        if not self.send_command(config_msg):
            self.log.insert("end", "[ERROR] Failed to send config!\n")
            self.log.see("end")
            return
        
        self.log.insert("end", "[DEBUG] Config sent successfully\n")
        self.log.see("end")

    def launch_backend(self):
        if not os.path.exists(C_EXECUTABLE):
            messagebox.showerror("Error", f"{C_EXECUTABLE} not found.")
            self.destroy()
            return
        try:
            self.c_process = subprocess.Popen([C_EXECUTABLE],
                                              stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
            self.log.insert("end", f"[INFO] C backend launched (PID {self.c_process.pid}). "
                                    f"Waiting for user 'Start'...\n")
            self.log.see("end")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch backend: {e}")
            self.destroy()

    def listen_backend(self):
        time.sleep(0.5)
        
        for attempt in range(100):
            try:
                self.sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1.0)
                self.socket_connected = True
                self.log.insert("end", f"[IPC] Connected to backend on attempt {attempt + 1}.\n")
                self.log.see("end")
                break
            except Exception as e:
                time.sleep(0.1)
        
        if not self.sock:
            self.log.insert("end", "[ERROR] Could not connect to backend after 100 attempts.\n")
            self.log.see("end")
            return
        else:
            self.log.insert("end", "[IPC] Socket ready – backend should now receive commands.\n")
            self.log.see("end")

        buf = ""
        message_count = 0
        while not self.stop_event.is_set():
            try:
                data = self.sock.recv(1024)
                if not data:
                    self.log.insert("end", "[IPC] Backend closed connection.\n")
                    self.log.see("end")
                    break
                
                buf += data.decode("utf-8", errors="ignore")
                
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip():
                        message_count += 1
                        self.log.insert("end", f"[RECV #{message_count}] {line.strip()}\n")
                        self.log.see("end")
                        self.after(0, self.process_msg, line.strip())
            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log.insert("end", f"[ERROR] Socket recv error: {e}\n")
                    self.log.see("end")
                break
        
        self.log.insert("end", f"[IPC] Connection closed. Total messages received: {message_count}\n")
        self.log.see("end")

    def process_msg(self, msg):
        parts = msg.split(',')
        if len(parts) < 4:
            return
        if parts[0] == "CONFIG" or parts[0] == "EXIT":
            return

        try:
            pid = int(parts[0])
            state = parts[1].upper()
            runway = int(parts[2])
            data_value = float(parts[3])
        except (ValueError, IndexError):
            self.log.insert("end", f"[WARNING] Malformed message: {msg}\n")
            self.log.see("end")
            return

        current_time = time.time() - self.sim_start_time

        item_id = plane_table_entries.get(pid)
        if not item_id:
            return
        
        try:
            prio = int(self.tree.set(item_id, "Prio"))
        except:
            prio = 0

        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] Plane {pid} -> {state} "
                                f"(Runway {runway}) [Prio: {prio}]\n")
        self.log.see("end")

        if pid not in plane_widgets:
            plane_widgets[pid] = {'duration': 0.0, 'progress': 0.0, 'state': state,
                                  'runway': runway, 'plane_id_label': None, 'segment_start': None}
        cfg = plane_widgets[pid]
        cfg['state'] = state
        cfg['runway'] = runway

        if state == "RUNNING":
            cfg['duration'] = data_value
            cfg['progress'] = 0.0
            cfg['segment_start'] = current_time
            self._clear_plane_widgets(pid)
            self._draw_plane(pid, runway)
            self.tree.set(item_id, "Runway", runway)
            self.tree.set(item_id, "Status", "RUNNING")

        elif state == "WAITING":
            cfg['progress'] = data_value
            if cfg.get('rect_type') != 'waiting':
                self._clear_plane_widgets(pid)
                self._draw_waiting(pid)
            self.tree.set(item_id, "Runway", 0)
            self.tree.set(item_id, "Status", "WAITING")

        elif state == "PROGRESS":
            cfg['progress'] = data_value
            self.tree.set(item_id, "Status", "PROGRESS")

        elif state == "COMPLETED":
            cfg['progress'] = 1.0
            self._finish_plane(pid)
            final_duration = cfg.get('duration', 0.0)
            segment_start = cfg.pop('segment_start', None)
            if segment_start is not None:
                global gantt_data
                gantt_data.append({'runway': runway, 'plane': pid,
                                   'start': segment_start, 'end': current_time,
                                   'duration': current_time - segment_start})
                self._update_gantt_chart()
            self.tree.set(item_id, "Runway", 0)
            self.tree.set(item_id, "Status", "COMPLETED")
            self.tree.set(item_id, "Elapsed", f"{int(final_duration)}s")

        duration = cfg.get('duration', 0.0)
        elapsed_s = int(cfg.get('progress', 0.0) * duration)
        self.tree.set(item_id, "Elapsed", f"{elapsed_s}s")

    def _draw_plane_shape(self, x, y, size, color, tags_list):
        f_x1, f_y1 = x - size * 1.5, y - size * 0.2
        f_x2, f_y2 = x + size * 2.5, y + size * 0.2
        self.canvas.create_rectangle(f_x1, f_y1, f_x2, f_y2, fill=color, outline=color, tags=tags_list)
        self.canvas.create_polygon(x + size * 0.5, y - size * 1.5,
                                   x - size * 0.5, y - size * 0.2,
                                   x - size * 0.5, y + size * 0.2,
                                   x + size * 0.5, y + size * 1.5,
                                   fill="#aeb0b3", outline="#000", tags=tags_list)
        tail_x = f_x2 - size * 0.2
        self.canvas.create_polygon(tail_x, f_y1,
                                   tail_x + size * 0.5, f_y1 - size * 0.8,
                                   tail_x + size * 0.5, f_y2,
                                   fill="#aeb0b3", outline="#000", tags=tags_list)
        self.canvas.create_oval(x - size * 2.5, y - size * 0.2,
                                x - size * 1.5, y + size * 0.2,
                                fill=color, outline="#000", tags=tags_list)
        return x, y

    def _draw_plane(self, pid, runway):
        self._clear_plane_widgets(pid)
        coords = self.runway_coords[runway - 1]
        x_center_init = coords[0] + 60
        y_center = (coords[1] + coords[3]) // 2
        tags = (f"p{pid}", f"p{pid} shape")
        self._draw_plane_shape(x_center_init, y_center, 10, "#4a90e2", tags)
        prio = int(self.tree.set(plane_table_entries[pid], "Prio"))
        txt = self.canvas.create_text(x_center_init - 35, y_center, text=f"P{pid} ({prio})",
                                      font=("Segoe UI", 8, "bold"), fill="#000",
                                      tags=(f"p{pid}", f"p{pid} text"))
        plane_widgets[pid]['txt_id'] = txt
        plane_widgets[pid]['rect_type'] = 'runway'

    def _draw_waiting(self, pid):
        self._clear_plane_widgets(pid)
        x_base = 20 + ((pid - 1) // NUM_PLANES) * 30
        y_base = 20 + ((pid - 1) % NUM_RUNWAYS) * 80 + 30
        tags = (f"p{pid}", f"p{pid} shape")
        self._draw_plane_shape(x_base, y_base, 5, "#f6e05e", tags)
        prio = int(self.tree.set(plane_table_entries[pid], "Prio"))
        txt = self.canvas.create_text(x_base, y_base - 10, text=f"P{pid} ({prio})",
                                      font=("Segoe UI", 7, "bold"), fill="#000",
                                      tags=(f"p{pid}", f"p{pid} text"))
        plane_widgets[pid]['txt_id'] = txt
        plane_widgets[pid]['rect_type'] = 'waiting'

    def _clear_plane_widgets(self, pid):
        self.canvas.delete(f"p{pid}")
        cfg = plane_widgets.get(pid, {})
        if 'txt_id' in cfg:
            del cfg['txt_id']

    def _finish_plane(self, pid):
        self.canvas.itemconfig(f"p{pid} shape", fill="#a0a0a0", outline="#505050")
        self.canvas.itemconfig(f"p{pid} text", fill="#000")

    def animate_planes(self):
        for pid, cfg in list(plane_widgets.items()):
            progress = cfg.get('progress', 0.0)
            state = cfg.get('state')
            runway = cfg.get('runway')
            if state in ("RUNNING", "PROGRESS") and runway > 0:
                coords = self.runway_coords[runway - 1]
                runway_start_x = coords[0] + 60
                runway_end_x = coords[2] - 10
                travel_distance = runway_end_x - runway_start_x
                x_target = runway_start_x + travel_distance * progress
                text_coords = self.canvas.coords(f"p{pid} text")
                if text_coords:
                    current_x = text_coords[0]
                    move_amount = x_target - current_x
                    self.canvas.move(f"p{pid}", move_amount, 0)
            elif state == "WAITING":
                jiggle_offset = int((progress * 20) % 5) - 2
                text_coords = self.canvas.coords(f"p{pid} text")
                if text_coords:
                    current_x = text_coords[0]
                    x_base = 20 + ((pid - 1) // NUM_PLANES) * 30
                    target_x = x_base + jiggle_offset
                    move_amount = target_x - current_x
                    self.canvas.move(f"p{pid}", move_amount, 0)
        self.animation_job_id = self.after(40, self.animate_planes)

    def on_close(self):
        self.stop_event.set()
        if self.animation_job_id:
            try:
                self.after_cancel(self.animation_job_id)
            except:
                pass
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        if self.c_process and self.c_process.poll() is None:
            self.c_process.terminate()
            try:
                self.c_process.wait(timeout=2)
            except:
                self.c_process.kill()
        self.destroy()


if __name__ == "__main__":
    app = AirportApp()
    app.mainloop()