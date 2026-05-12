import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import os
import time
import threading
import struct
import random
import hashlib
import socket
import pickle

try:
    import xxhash

    def _state_fingerprint(obj: object) -> str:
        return xxhash.xxh64(pickle.dumps(obj, protocol=4)).hexdigest()
except ImportError:
    def _state_fingerprint(obj: object) -> str:
        return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()

# Main window caption (Tk title bar)
CATHLE_WINDOW_TITLE = "cathle 0.1 [C] A.C Holdings 1999-2026 [C] Nintendo 1985-2026"

TARGET_FPS = 60.0
VERBOSE_EMU = False


class RealityCoprocessor:
    def __init__(self, canvas):
        self.canvas = canvas
        self.framebuffer = [[0] * 320 for _ in range(240)]  # 320x240 framebuffer
        self.rsp_active = False
        self.rdp_active = False
        self.cycle_count = 0
        self.vu_registers = [0] * 32  # Vector Unit registers
        self._pixel_tag = "emuai4k_pixels"
        self.rsp_pc = 0
        self.rsp_status = 0
        self.rsp_memory = [0] * 0x1000  # 4KB RSP DMEM
        self.use_test_pattern = True  # Toggle for test pattern
        self.freeze_raster = False  # When True, RSP does not overwrite framebuffer (toy in-game scene)
        self.suppress_pixel_push = False  # When True, skip RSP full-frame fill (black boot window)

    def rsp_execute(self, rom_data, pc):
        """Execute RSP instructions, with SM64-compatible ops."""
        if VERBOSE_EMU:
            print(f"[RSP] Executing at PC: {hex(pc)}")
        self.rsp_active = True
        self.cycle_count += 10
        try:
            if pc + 4 > len(rom_data):
                raise ValueError("PC out of ROM bounds")
            opcode = struct.unpack('>I', rom_data[pc:pc+4])[0]
            op = (opcode >> 26) & 0x3F
            if VERBOSE_EMU:
                print(f"[RSP] Opcode: {hex(opcode)}, Op: {hex(op)}")
            if op == 0x32:  # VADD
                rs = (opcode >> 21) & 0x1F
                rt = (opcode >> 16) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = (self.vu_registers[rs] + self.vu_registers[rt]) & 0xFFFF
                self.cycle_count += 8
                if VERBOSE_EMU:
                    print(f"[RSP] VADD: vu[{rd}] = vu[{rs}] + vu[{rt}] = {self.vu_registers[rd]}")
            elif op == 0x33:  # VMUL
                rs = (opcode >> 21) & 0x1F
                rt = (opcode >> 16) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = (self.vu_registers[rs] * self.vu_registers[rt]) & 0xFFFF
                self.cycle_count += 10
                if VERBOSE_EMU:
                    print(f"[RSP] VMUL: vu[{rd}] = vu[{rs}] * vu[{rt}] = {self.vu_registers[rd]}")
            elif op == 0x34:  # VMOV (SM64 uses similar vector ops)
                rs = (opcode >> 21) & 0x1F
                rd = (opcode >> 11) & 0x1F
                self.vu_registers[rd] = self.vu_registers[rs]
                self.cycle_count += 6
                if VERBOSE_EMU:
                    print(f"[RSP] VMOV: vu[{rd}] = vu[{rs}] = {self.vu_registers[rd]}")
            # Generate pixel data for SM64 (skipped once toy "in-level" frame is shown)
            if not self.freeze_raster and not self.suppress_pixel_push:
                for i in range(320):
                    for j in range(240):
                        self.framebuffer[j][i] = (
                            (self.vu_registers[0] % 256) << 16
                            | (self.vu_registers[1] % 256) << 8
                            | (self.vu_registers[2] % 256)
                        )
            self.rsp_memory[self.rsp_pc % 0x1000] = opcode & 0xFFFF
            self.rsp_pc += 4
            if VERBOSE_EMU:
                print(f"[RSP] Framebuffer sample: {self.framebuffer[0][0]:08x}")
        except Exception as e:
            print(f"[RSP] Error: {e}")
        self.rsp_active = False
        return pc + 4

    def rdp_render(self):
        """Render framebuffer (tk main thread). Skips 76k rectangles when fully black for 60fps boot."""
        self.rdp_active = True
        pixel_size = 2
        self.canvas.delete(self._pixel_tag)
        has_nonzero = any(any(pixel != 0 for pixel in row) for row in self.framebuffer)
        if not has_nonzero and not self.use_test_pattern:
            self.rdp_active = False
            self.cycle_count += 1
            return
        if VERBOSE_EMU:
            print("[RDP] Rendering framebuffer")
        if not has_nonzero and self.use_test_pattern:
            if VERBOSE_EMU:
                print("[RDP] Framebuffer empty, rendering test pattern")
            for y in range(240):
                for x in range(320):
                    r = (x * 255 // 320) % 256
                    g = (y * 255 // 240) % 256
                    b = ((x + y) * 255 // 560) % 256
                    self.framebuffer[y][x] = (r << 16) | (g << 8) | b
        for y in range(240):
            for x in range(320):
                color = self.framebuffer[y][x]
                r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
                hex_color = "#%02x%02x%02x" % (r, g, b)
                self.canvas.create_rectangle(
                    x * pixel_size, y * pixel_size,
                    (x + 1) * pixel_size, (y + 1) * pixel_size,
                    fill=hex_color, outline="",
                    tags=(self._pixel_tag,),
                )
        self.rdp_active = False
        self.cycle_count += 1000

class N64Emulator:
    def __init__(self, root):
        self.root = root
        # Project 640 · 0.1 — tkinter shell; 320×240 framebuffer scaled ×2 → 640×480
        self.root.title(CATHLE_WINDOW_TITLE)
        self.root.geometry("720x640")
        self.root.minsize(680, 600)
        self.root.configure(bg="#d4d0c8")
        self.is_running = False
        self.rom_path = None
        self.rom_title = "Unknown"
        self.rom_data = b""
        self.rom_md5 = ""
        self.emulation_thread = None
        self.emulation_speed = 1.0
        self.frame_count = 0
        self.pc = 0x1000  # ROM file offset for this toy fetch-decode path
        self._is_sm64_rom = False
        self._is_commercial_cart = False  # valid cartridge ROM after header byteswap
        self._boot_phase = 0  # frames since last boot (black → SM64 toy in-game)
        self._pending_soft_reset = False
        self.save_states = {}
        self.cheat_codes = {}
        self.controller_config = {
            "Up": "w", "Down": "s", "Left": "a", "Right": "d",
            "A": "j", "B": "k", "Start": "Return", "Z": "z"
        }
        self.cpu_registers = [0] * 32
        self.rcp = None
        self.core_installed = False
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.rdram = [0] * 0x400000
        self.rdram_size = 0x400000
        self.plugins = {
            "video": "glide64mk2",
            "audio": "sdl",
            "input": "sdl",
            "rsp": "hle"
        }
        self.netplay_enabled = False
        self.netplay_socket = None
        self.auto_install_core()
        self.setup_gui()

    def auto_install_core(self):
        self.core_installed = True
        print(f"[Core] Mupen64Plus 2.6.0 core installed to: {self.script_dir} (simulated)")
        print(f"[Core] Plugins: {self.plugins}")

    def setup_gui(self):
        menubar = tk.Menu(self.root, bg="#d4d0c8", font=("Arial", 10))
        file_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        file_menu.add_command(label="Open ROM...", command=self.load_rom)
        file_menu.add_command(label="ROM Information", command=self.show_rom_info)
        file_menu.add_command(label="Open 64DD Disk", command=self.load_64dd_disk)
        file_menu.add_command(label="Toggle Test Pattern", command=self.toggle_test_pattern)
        file_menu.add_separator()
        file_menu.add_command(label="End Emulation", command=self.stop_emulation)
        file_menu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)
        system_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        system_menu.add_command(label="Save State", command=self.save_state)
        system_menu.add_command(label="Load State", command=self.load_state)
        system_menu.add_command(label="Pause", command=self.pause_emulation)
        system_menu.add_command(label="Reset", command=self.reset_emulation)
        system_menu.add_command(label="Soft Reset", command=self.soft_reset)
        menubar.add_cascade(label="System", menu=system_menu)
        options_menu = tk.Menu(menubar, tearoff=0, bg="#d4d0c8", font=("Arial", 10))
        options_menu.add_command(label="Configure Controller", command=self.configure_controller)
        options_menu.add_command(label="Set Emulation Speed", command=self.set_emulation_speed)
        options_menu.add_command(label="Cheat Codes", command=self.add_cheat_code)
        options_menu.add_command(label="Select Plugins", command=self.select_plugins)
        options_menu.add_command(label="Enable Netplay", command=self.toggle_netplay)
        menubar.add_cascade(label="Options", menu=options_menu)
        self.root.config(menu=menubar)
        main_frame = tk.Frame(self.root, bg="#d4d0c8")
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        browser_frame = tk.Frame(main_frame, bg="#d4d0c8", relief="sunken", borderwidth=2)
        browser_frame.pack(fill="x", padx=5, pady=5)
        tk.Label(browser_frame, text="ROM Browser", bg="#d4d0c8", font=("Arial", 10, "bold")).pack(anchor="w")
        self.rom_listbox = tk.Listbox(browser_frame, height=5, bg="white", font=("Arial", 10))
        self.rom_listbox.pack(fill="x", padx=5, pady=5)
        self.rom_listbox.bind("<<ListboxSelect>>", self.select_rom)
        self.canvas = tk.Canvas(main_frame, width=640, height=480, bg="black", relief="sunken", borderwidth=2)
        self.canvas.pack(pady=10)
        self.rcp = RealityCoprocessor(self.canvas)
        self.canvas.frame_count = self.frame_count
        control_frame = tk.Frame(main_frame, bg="#d4d0c8")
        control_frame.pack(fill="x")
        tk.Button(control_frame, text="Start", command=self.start_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        tk.Button(control_frame, text="Pause", command=self.pause_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        tk.Button(control_frame, text="Stop", command=self.stop_emulation, bg="#c0c0c0", relief="raised", font=("Arial", 10)).pack(side="left", padx=5)
        self.status_label = tk.Label(main_frame, text="No ROM loaded", bg="#d4d0c8", font=("Arial", 10))
        self.status_label.pack(pady=5)
        self.bind_controls()
        self.update_rom_browser()

    def _gui_safe(self, fn):
        """Run Tk updates on the main thread only (never call Tk from worker threads)."""
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.root.after(0, fn)

    def update_rom_browser(self):
        self.rom_listbox.delete(0, tk.END)
        for file in os.listdir(self.script_dir):
            if file.lower().endswith((".n64", ".z64", ".v64", ".ndd")):
                self.rom_listbox.insert(tk.END, file)

    def select_rom(self, event):
        selection = self.rom_listbox.curselection()
        if selection:
            file_name = self.rom_listbox.get(selection[0])
            self.rom_path = os.path.join(self.script_dir, file_name)
            if file_name.lower().endswith(".ndd"):
                self.load_64dd_disk()
            else:
                self.load_rom()

    def bind_controls(self):
        for seq in getattr(self, "_control_bindings", ()):
            try:
                self.root.unbind(seq)
            except tk.TclError:
                pass
        self._control_bindings = []
        for action, key in self.controller_config.items():
            seq = f"<KeyPress-{key}>"
            self.root.bind(seq, lambda e, a=action: self.handle_input(a))
            self._control_bindings.append(seq)

    def handle_input(self, action):
        if self.is_running:
            self.cpu_registers[5] = hash(action) % 0xFFFF
            self.status_label.config(text=f"Input: {action} pressed! | Game: {self.rom_title}")
            print(f"[Input] {action} pressed, r[5] = {self.cpu_registers[5]}")
            if self.netplay_enabled and self.netplay_socket:
                try:
                    self.netplay_socket.send(action.encode())
                except OSError:
                    print("[Netplay] Input send failed")

    def show_rom_info(self):
        if self.rom_path:
            messagebox.showinfo("ROM Info", f"Title: {self.rom_title}\nPath: {self.rom_path}\nRDRAM Size: {self.rdram_size//1024}KB\nROM Size: {len(self.rom_data)} bytes\nMD5: {self.rom_md5}")
        else:
            messagebox.showwarning("No ROM", "No ROM or 64DD disk loaded, CatSama!")

    def toggle_test_pattern(self):
        self.rcp.use_test_pattern = not self.rcp.use_test_pattern
        state = "enabled" if self.rcp.use_test_pattern else "disabled"
        messagebox.showinfo("Test Pattern", f"Test pattern {state}, CatSama!")
        print(f"[RDP] Test pattern {state}")

    def load_rom(self):
        if not self.core_installed:
            messagebox.showwarning("Core Not Installed", "Mupen64Plus core is missing, CatSama!")
            return
        if not self.rom_path:
            self.rom_path = filedialog.askopenfilename(filetypes=[("N64 ROMs", "*.n64 *.z64 *.v64")])
        if self.rom_path:
            try:
                with open(self.rom_path, 'rb') as f:
                    self.rom_data = f.read()
                if len(self.rom_data) < 64:
                    raise ValueError("ROM too small")
                print(f"[ROM] Loaded ROM: {self.rom_path}, Size: {len(self.rom_data)} bytes")
                header = self.rom_data[:64]
                if header[0:4] == b'\x80\x37\x12\x40':
                    print("[ROM] Big-endian (.z64)")
                elif header[0:4] == b'\x37\x80\x40\x12':
                    bswap = bytearray(self.rom_data)
                    for i in range(0, len(bswap) - 1, 2):
                        bswap[i], bswap[i + 1] = bswap[i + 1], bswap[i]
                    self.rom_data = bytes(bswap)
                    print("[ROM] Byte-swapped (.v64)")
                elif header[0:4] == b'\x40\x12\x37\x80':
                    self.rom_data = self.rom_data[::-1]
                    print("[ROM] Little-endian (.n64)")
                else:
                    raise ValueError("Invalid N64 ROM header")
                self.rom_title = self.rom_data[0x20:0x34].decode('ascii', errors='ignore').strip() or os.path.basename(self.rom_path)
                self.rom_md5 = hashlib.md5(self.rom_data).hexdigest()
                print(f"[ROM] Title: {self.rom_title}, MD5: {self.rom_md5}")
                self.status_label.config(text=f"ROM Loaded: {self.rom_title}")
                self.canvas.delete("all")
                self.rdram = [0] * self.rdram_size
                self._is_sm64_rom = "mario" in self.rom_title.lower()
                self._is_commercial_cart = True
                self._apply_commercial_boot(clear_fb=True)
                print(f"[ROM] vu_registers: {self.rcp.vu_registers[:3]}")
            except Exception as e:
                messagebox.showerror("ROM Error", f"Failed to load ROM: {str(e)}")
                print(f"[ROM] Error: {str(e)}")
                self.rom_path = None
                self.rom_data = b""
                self.rom_md5 = ""
                self._is_sm64_rom = False
                self._is_commercial_cart = False

    def load_64dd_disk(self):
        if not self.core_installed:
            messagebox.showwarning("Core Not Installed", "Mupen64Plus core is missing, CatSama!")
            return
        if not self.rom_path:
            self.rom_path = filedialog.askopenfilename(filetypes=[("64DD Disks", "*.ndd")])
        if self.rom_path:
            try:
                with open(self.rom_path, 'rb') as f:
                    self.rom_data = f.read()
                self.rom_title = os.path.basename(self.rom_path)
                print(f"[64DD] Loaded disk: {self.rom_title}, Size: {len(self.rom_data)} bytes")
                self.status_label.config(text=f"64DD Disk Loaded: {self.rom_title}")
                self.canvas.delete("all")
                self.pc = 0x1000
                self.rcp.vu_registers[0] = random.randint(1, 255)
                self.rcp.vu_registers[1] = random.randint(1, 255)
                self.rcp.vu_registers[2] = random.randint(1, 255)
                print(f"[64DD] vu_registers: {self.rcp.vu_registers[:3]}")
            except Exception as e:
                messagebox.showerror("64DD Error", f"Failed to load 64DD disk: {str(e)}")
                print(f"[64DD] Error: {str(e)}")
                self.rom_path = None

    def start_emulation(self):
        if not self.core_installed:
            messagebox.showwarning("Core Not Installed", "Mupen64Plus core is missing, CatSama!")
            return
        if not self.rom_path:
            messagebox.showwarning("No ROM", "Please load a ROM or 64DD disk first, CatSama!")
            return
        if not self.is_running:
            self.is_running = True
            self.emulation_thread = threading.Thread(target=self.emulation_loop)
            self.emulation_thread.daemon = True
            self.emulation_thread.start()
            self.status_label.config(text=f"Emulating {self.rom_title}! Let's rock, CatSama!")
            print("[Emulation] Started")

    def pause_emulation(self):
        if self.is_running:
            self.is_running = False
            self.status_label.config(text="Emulation Paused")
            print("[Emulation] Paused")

    def stop_emulation(self):
        self.is_running = False
        self.frame_count = 0
        self.pc = 0x1000
        self.canvas.delete("all")
        self.status_label.config(text="Emulation Stopped")
        print("[Emulation] Stopped")
        if self.netplay_socket:
            self.netplay_socket.close()
            self.netplay_socket = None

    def reset_emulation(self):
        self.stop_emulation()
        self.cpu_registers = [0] * 32
        self.rcp.vu_registers = [0] * 32
        self.rcp.rsp_memory = [0] * 0x1000
        self.rdram = [0] * self.rdram_size
        self.status_label.config(text="Emulation Reset")
        print("[Emulation] Hard Reset")
        if self._is_commercial_cart:
            self._apply_commercial_boot(clear_fb=True)
            print("[Reset] Commercial cart toy boot state restored")

    def soft_reset(self):
        """Soft reset: replay black-window boot for any commercial cartridge (toy CPU)."""
        if self._is_commercial_cart:
            self._pending_soft_reset = True
            self.status_label.config(text="Soft reset — replaying cart boot (black window)…")
            print("[Emulation] Soft reset (commercial cart)")
        else:
            self._normalize_rom_pc(force_entry=True)
            self.status_label.config(text="Soft Reset")
            print("[Emulation] Soft Reset")

    def _normalize_rom_pc(self, force_entry: bool = False) -> None:
        """Keep fetch PC inside the ROM image — real SM64 jumps to RDRAM; this toy stays in-cart."""
        if len(self.rom_data) < 0x1010:
            self.pc = 0
            return
        lo, hi = 0x1000, len(self.rom_data) - 4
        if force_entry or self.pc < lo or self.pc > hi or (self.pc & 3) != 0:
            self.pc = lo

    def _apply_commercial_boot(self, clear_fb: bool = False) -> None:
        """All valid cartridge ROMs: black window first, PC at 0x1000, stable 60fps path."""
        self.pc = 0x1000
        self._boot_phase = 0
        self.rcp.freeze_raster = False
        self.rcp.suppress_pixel_push = True
        self.rcp.use_test_pattern = False
        self._normalize_rom_pc(force_entry=True)
        if clear_fb:
            self.rcp.framebuffer = [[0] * 320 for _ in range(240)]
        self._sm64_game_visual_done = False
        if self._is_sm64_rom:
            self.cpu_registers = [0] * 32
            self.cpu_registers[29] = 0x8033B400
            self.rcp.vu_registers[0] = 0xFF
            self.rcp.vu_registers[1] = 0xA0
            self.rcp.vu_registers[2] = 0x00
            print("[ROM] SM64 toy boot: black window then RSP tint (not a real N64 core)")
        else:
            self.cpu_registers = [0] * 32
            self.cpu_registers[29] = 0x8033B400
            h = int(self.rom_md5[:8], 16) if len(self.rom_md5) >= 8 else 0
            self.rcp.vu_registers[0] = (h >> 16) & 0xFF
            self.rcp.vu_registers[1] = (h >> 8) & 0xFF
            self.rcp.vu_registers[2] = h & 0xFF
            print("[ROM] Commercial cart toy boot: black window @ 60fps (toy shell)")

    def _commercial_boot_tick(self) -> None:
        """Per-frame boot timeline: non-SM64 carts stay black; SM64 unlocks RSP then toy level."""
        if not self._is_commercial_cart:
            return
        self._boot_phase += 1
        if self._is_sm64_rom:
            if self._boot_phase == 12:
                self.rcp.suppress_pixel_push = False
            if self._boot_phase == 45:
                self.status_label.config(text="SM64: entering game (toy display)…")
            if self._boot_phase >= 90:
                self.rcp.use_test_pattern = False
                if not self._sm64_game_visual_done:
                    self._sm64_game_visual_done = True
                    for y in range(240):
                        for x in range(320):
                            sky_b = min(255, 90 + int(y * 0.55))
                            sky_g = min(255, 120 + int(y * 0.35))
                            if y >= 168:
                                gnd = min(255, 40 + (y - 168) * 8 + (x % 17))
                                r, g, b = gnd, gnd // 2, gnd // 4
                            else:
                                r, g, b = 32, sky_g, sky_b
                            self.rcp.framebuffer[y][x] = (r << 16) | (g << 8) | b
                    self.rcp.vu_registers[0] = 0x40
                    self.rcp.vu_registers[1] = 0xA0
                    self.rcp.vu_registers[2] = 0xE0
                    self.rcp.freeze_raster = True
                    print("[SM64] Toy in-game frame (use a real N64 core for actual gameplay)")
        else:
            # Other commercial ROMs: keep black viewport; no test pattern, no RSP full-frame push
            self.rcp.suppress_pixel_push = True
            if self._boot_phase == 1:
                self.status_label.config(text=f"{self.rom_title[:28]} — boot (black) @ {int(TARGET_FPS)}fps toy")

    def emulation_loop(self):
        dynarec_cache: dict[int, object] = {}
        while self.is_running and len(self.rom_data) >= 0x1010:
            if self._pending_soft_reset:
                self._pending_soft_reset = False
                dynarec_cache.clear()
                self._apply_commercial_boot(clear_fb=True)
                print("[CPU] Soft reset — dynarec cleared, commercial boot replay")

            self.frame_count += 1
            loop_t0 = time.perf_counter()
            try:
                self._normalize_rom_pc()
                pc_fetch = self.pc
                opcode = struct.unpack(">I", self.rom_data[pc_fetch : pc_fetch + 4])[0]
                op = (opcode >> 26) & 0x3F
                if pc_fetch not in dynarec_cache:
                    if op == 0:  # ADD
                        rs = (opcode >> 21) & 0x1F
                        rt = (opcode >> 16) & 0x1F
                        rd = (opcode >> 11) & 0x1F
                        dynarec_cache[pc_fetch] = lambda rs=rs, rt=rt, rd=rd: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rd]
                            + [(self.cpu_registers[rs] + self.cpu_registers[rt]) & 0xFFFFFFFF]
                            + self.cpu_registers[rd + 1 :],
                        )
                    elif op == 0x2B:  # SW
                        rt = (opcode >> 16) & 0x1F
                        dynarec_cache[pc_fetch] = lambda rt=rt: setattr(
                            self.rcp,
                            "vu_registers",
                            self.rcp.vu_registers[:0] + [self.cpu_registers[rt]] + self.rcp.vu_registers[1:],
                        )
                    elif op == 0x23:  # LW
                        rt = (opcode >> 16) & 0x1F
                        imm = opcode & 0xFFFF
                        dynarec_cache[pc_fetch] = lambda rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt]
                            + [self.rdram[imm % self.rdram_size]]
                            + self.cpu_registers[rt + 1 :],
                        )
                    elif op == 0x02:  # J
                        target = (opcode & 0x3FFFFFF) << 2
                        pc_here = pc_fetch

                        def _j():
                            self.pc = (pc_here & 0xF0000000) | target
                            self._normalize_rom_pc()

                        dynarec_cache[pc_fetch] = _j
                    elif op == 0x03:  # JAL
                        target = (opcode & 0x3FFFFFF) << 2
                        pc_here = pc_fetch
                        ra_val = (pc_here + 8) & 0xFFFFFFFF
                        new_pc = (pc_here & 0xF0000000) | target

                        def _jal():
                            regs = list(self.cpu_registers)
                            regs[31] = ra_val
                            self.cpu_registers = regs
                            self.pc = new_pc
                            self._normalize_rom_pc()

                        dynarec_cache[pc_fetch] = _jal
                    elif op == 0x0F:  # LUI
                        rt = (opcode >> 16) & 0x1F
                        imm = (opcode & 0xFFFF) << 16
                        dynarec_cache[pc_fetch] = lambda rt=rt, imm=imm: setattr(
                            self,
                            "cpu_registers",
                            self.cpu_registers[:rt] + [imm] + self.cpu_registers[rt + 1 :],
                        )
                    else:
                        dynarec_cache[pc_fetch] = lambda: None

                dynarec_cache[pc_fetch]()
                if op == 0x2B:
                    self.pc = self.rcp.rsp_execute(self.rom_data, self.pc)
                    self._normalize_rom_pc()
                elif op not in (0x02, 0x03):
                    self.pc += 4
                    self._normalize_rom_pc()

                if self.cheat_codes:
                    code = next(iter(self.cheat_codes))
                    try:
                        self.cpu_registers[1] = int(code, 16) & 0xFFFF
                    except ValueError:
                        pass

                if self.netplay_enabled and self.netplay_socket:
                    try:
                        self.netplay_socket.setblocking(False)
                        data = self.netplay_socket.recv(1024)
                        if data:
                            self.cpu_registers[6] = int.from_bytes(data[:8], "big") & 0xFFFF
                    except BlockingIOError:
                        pass
                    except OSError:
                        pass

                self._sm64_boot_tick()

                fc, pcv, title = self.frame_count, self.pc, self.rom_title

                def _present():
                    self.rcp.rdp_render()
                    phase = f" | boot {self._boot_phase}" if self._is_sm64_rom else ""
                    self.status_label.config(
                        text=f"Frame: {fc} | PC: {hex(pcv)} | {title}{phase}"
                    )

                self._gui_safe(_present)
                time.sleep(1 / (60 * self.emulation_speed))
            except Exception as e:
                print(f"[CPU] Error: {e}")
                self._normalize_rom_pc(force_entry=True)
                self._gui_safe(lambda err=str(e): self.status_label.config(text=f"CPU: {err}"))

    def save_state(self):
        if not self.rom_path:
            messagebox.showwarning("No ROM", "No ROM loaded to save state, CatSama!")
            return
        slot = simpledialog.askinteger("Save State", "Enter slot number (1-10):", minvalue=1, maxvalue=10)
        if slot:
            state_data = {
                "frame_count": self.frame_count,
                "pc": self.pc,
                "cpu_registers": self.cpu_registers[:],
                "rom_title": self.rom_title,
                "framebuffer": [row[:] for row in self.rcp.framebuffer],
                "rdram": self.rdram[:],
                "rsp_memory": self.rcp.rsp_memory[:]
            }
            state_hash = _state_fingerprint(state_data)
            self.save_states[slot] = state_data
            messagebox.showinfo("Saved", f"State saved to slot {slot}! Hash: {state_hash} Keep rocking, CatSama!")
            print(f"[Save] State saved to slot {slot}, Hash: {state_hash}")

    def load_state(self):
        if not self.rom_path:
            messagebox.showwarning("No ROM", "No ROM loaded to load state, CatSama!")
            return
        slot = simpledialog.askinteger("Load State", "Enter slot number (1-10):", minvalue=1, maxvalue=10)
        if slot in self.save_states:
            state = self.save_states[slot]
            state_hash = _state_fingerprint(state)
            self.frame_count = state["frame_count"]
            self.pc = state["pc"]
            self.cpu_registers = state["cpu_registers"][:]
            self.rom_title = state["rom_title"]
            self.rcp.framebuffer = [row[:] for row in state["framebuffer"]]
            self.rdram = state["rdram"][:]
            self.rcp.rsp_memory = state["rsp_memory"][:]
            self.status_label.config(text=f"Loaded state from slot {slot}! Hash: {state_hash} Game: {self.rom_title}")
            print(f"[Load] State loaded from slot {slot}, Hash: {state_hash}")
        else:
            messagebox.showwarning("No State", f"No state found in slot {slot}, CatSama!")

    def configure_controller(self):
        for action in self.controller_config:
            key = simpledialog.askstring("Controller Config", f"Enter key for {action}:", initialvalue=self.controller_config[action])
            if key:
                self.controller_config[action] = key
        self.bind_controls()
        messagebox.showinfo("Controller", "Controller updated! Ready for action, CatSama!")

    def set_emulation_speed(self):
        speed = simpledialog.askfloat("Emulation Speed", "Enter speed (0.5 to 20.0):", minvalue=0.5, maxvalue=20.0)
        if speed:
            self.emulation_speed = speed
            self.status_label.config(text=f"Emulation speed set to {speed}x")
            print(f"[Emulation] Speed set to {speed}x")

    def add_cheat_code(self):
        code = simpledialog.askstring("Cheat Code", "Enter cheat code (hex):")
        if code:
            self.cheat_codes[code] = True
            messagebox.showinfo("Cheat", f"Cheat code {code} activated! Let's break the game, CatSama!")
            print(f"[Cheat] Added code: {code}")

    def select_plugins(self):
        plugin_types = ["video", "audio", "input", "rsp"]
        plugin_options = {
            "video": ["glide64mk2", "rice", "arachnoid", "z64"],
            "audio": ["sdl", "jttl_audio"],
            "input": ["sdl"],
            "rsp": ["hle", "cxd4", "z64"]
        }
        for ptype in plugin_types:
            plugin = simpledialog.askstring("Plugin Config", f"Select {ptype} plugin:", initialvalue=self.plugins[ptype])
            if plugin in plugin_options[ptype]:
                self.plugins[ptype] = plugin
        messagebox.showinfo("Plugins", f"Plugins updated: {self.plugins}! Ready to roll, CatSama!")
        print(f"[Plugins] Updated: {self.plugins}")

    def toggle_netplay(self):
        if not self.netplay_enabled:
            host = simpledialog.askstring("Netplay", "Enter host IP (leave blank for server):")
            self.netplay_enabled = True
            try:
                self.netplay_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.netplay_socket.setblocking(False)
                if host:
                    self.netplay_socket.connect((host, 45000))
                    messagebox.showinfo("Netplay", f"Connected to {host}! Let's sync, CatSama!")
                    print(f"[Netplay] Connected to {host}:45000")
                else:
                    self.netplay_socket.bind(("0.0.0.0", 45000))
                    messagebox.showinfo("Netplay", "Netplay server started! Waiting for players, CatSama!")
                    print("[Netplay] Server started on port 45000")
            except Exception as e:
                messagebox.showerror("Netplay Error", f"Failed to start netplay: {str(e)}")
                print(f"[Netplay] Error: {str(e)}")
                self.netplay_enabled = False
                self.netplay_socket = None
        else:
            self.netplay_enabled = False
            if self.netplay_socket:
                self.netplay_socket.close()
                self.netplay_socket = None
            messagebox.showinfo("Netplay", "Netplay disabled! Solo gaming time, CatSama!")
            print("[Netplay] Disabled")

if __name__ == "__main__":
    print("12ABKKK2NNAAAA THE YOSHI DUDES ARE HEREE WA WA")
    root = tk.Tk()
    emulator = N64Emulator(root)
    root.mainloop()
