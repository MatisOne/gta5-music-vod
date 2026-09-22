"""
gta-radio-music v5 (dopasowanie po potwierdzonym wzorze hasha + pauza + config)
================================================================================
Czyta plik statusu (gta_radio_status.json) zapisywany przez gta_radio_bridge.asi.
Dopasowuje utwór 1:1 po hashu joaat liczonym z połączonego stringa
"<nazwa_stacji>_<nazwa_utworu>" (oba w lowercase) - wzór potwierdzony
empirycznie i zweryfikowany na realnych danych z gry.

Nowości w v5:
- Usunięty cały ręczny system (hash_map.txt / oznaczanie utworu) - niepotrzebny,
  odkąd wzór hasha jest pewny i liczony automatycznie.
- Zrzut hashy do pliku debug jest teraz na żądanie (przycisk), nie przy
  każdym skanowaniu folderu.
- Wykrywanie pauzy: jeśli playback_ms z gry przestaje się zmieniać (gra w
  pauzie / radio nieaktywne), program usypia lokalne odtwarzanie zamiast
  bez końca "doskakiwać" do tej samej pozycji (co dawało efekt zapętlenia
  ułamka sekundy).
- Config: zapamiętuje wskazany plik statusu i dodane foldery stacji między
  uruchomieniami (gta_radio_config.json obok programu).

Wymagania:
    pip install soundfile sounddevice numpy

Budowanie .exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name gta-radio-music gta_radio_music_v5.py
"""

import os
import sys
import json
import glob
import time
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

import soundfile as sf
import sounddevice as sd

STATUS_POLL_INTERVAL_SEC = 0.15
DRIFT_CORRECTION_THRESHOLD_MS = 350
PAUSE_DETECT_THRESHOLD_SEC = 1.0


def get_base_dir():
    """Folder programu - działa zarówno uruchomiony jako .py jak i jako
    zbudowany .exe (pyinstaller)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(get_base_dir(), "gta_radio_config.json")


def joaat(s: str) -> int:
    """Jenkins one-at-a-time hash (lowercase), pełny zakres 32-bit.
    Wejście to połączony string "<stacja>_<utwór>" - wzór potwierdzony
    na realnych danych z gry (np. "radio_01_class_rock_i_dont_care_anymore")."""
    data = s.lower().encode("utf-8")
    h = 0
    for c in data:
        h = (h + c) & 0xFFFFFFFF
        h = (h + ((h << 10) & 0xFFFFFFFF)) & 0xFFFFFFFF
        h ^= (h >> 6)
    h = (h + ((h << 3) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h ^= (h >> 11)
    h = (h + ((h << 15) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


class StationLibrary:
    """Biblioteka plików jednej stacji, zaindeksowana po hashu
    joaat(f"{nazwa_stacji}_{nazwa_utworu}")."""

    def __init__(self, name, folder):
        self.name = name
        self.folder = folder
        self.by_hash = {}
        self._scan()
        self._load_overrides()

    def _scan(self):
        pattern = os.path.join(self.folder, "**", "*.wav")
        files = glob.glob(pattern, recursive=True)
        self.by_hash = {}
        for f in files:
            base = os.path.splitext(os.path.basename(f))[0]
            sound_name = f"{self.name}_{base}"
            self.by_hash[joaat(sound_name)] = f

    def _load_overrides(self):
        """Wczytuje ręczne dowiązania hash=ścieżka z overrides.txt w folderze
        stacji - dla wyjątków, których automatyczny wzór "stacja_utwór" nie
        obejmuje (np. niektóre stacje DLC z innym nazewnictwem wewnętrznym).
        Format pliku (jedna linia = jedno dowiązanie), np.:
            3734589094=lab_p1/DLC_THELAB_LAB_P1.wav
        Ścieżka jest względna wobec folderu stacji. Te wpisy MAJĄ
        pierwszeństwo przed automatycznie obliczonym hashem."""
        override_path = os.path.join(self.folder, "overrides.txt")
        if not os.path.exists(override_path):
            return
        with open(override_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                hash_str, rel_path = line.split("=", 1)
                hash_str = hash_str.strip()
                rel_path = rel_path.strip()
                try:
                    h = int(hash_str, 16) if hash_str.lower().startswith("0x") else int(hash_str)
                except ValueError:
                    print(f"[overrides.txt:{line_no}] nieprawidłowy hash: '{hash_str}' - pomijam")
                    continue
                full_path = os.path.join(self.folder, rel_path)
                if not os.path.exists(full_path):
                    print(f"[overrides.txt:{line_no}] plik nie istnieje: '{full_path}' - pomijam")
                    continue
                self.by_hash[h & 0xFFFFFFFF] = full_path

    def find(self, track_hash):
        return self.by_hash.get(track_hash)

    def random_file(self):
        if not self.by_hash:
            return None
        import random
        return random.choice(list(self.by_hash.values()))

    def dump_debug_hashes(self):
        debug_path = os.path.join(self.folder, "computed_hashes_debug.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            for h, path in sorted(self.by_hash.items()):
                f.write(f"{h}\t{os.path.relpath(path, self.folder)}\n")
        return debug_path


class SyncPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("gta-radio-music v5 (sync po hashu)")
        self.root.geometry("500x400")
        self.root.resizable(False, False)

        self.stations = {}  # station_internal_name -> StationLibrary
        self.status_file_path = tk.StringVar(value="")
        self.volume = tk.DoubleVar(value=0.8)
        self.running = False
        self.poll_thread = None

        self._audio_data = None
        self._samplerate = None
        self._play_pos = 0
        self._lock = threading.Lock()
        self._stream = None
        self._paused_by_game = False

        self._build_ui()
        self._load_config()

    # ---------- UI ----------

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Button(
            frame, text="Wskaż gta_radio_status.json (z folderu gry)", command=self.choose_status_file
        ).pack(fill=tk.X)
        self.status_path_label = ttk.Label(frame, text="(nie wybrano)", foreground="#555555")
        self.status_path_label.pack(anchor="w", pady=(2, 8))

        station_btns = ttk.Frame(frame)
        station_btns.pack(fill=tk.X)
        ttk.Button(station_btns, text="+ Dodaj stację (folder = ID stacji z gry)", command=self.add_station).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(station_btns, text="Debug: zrzuć hashe", command=self.dump_all_debug).pack(side=tk.LEFT, padx=(6, 0))

        self.station_listbox = tk.Listbox(frame, height=5)
        self.station_listbox.pack(fill=tk.X, pady=(6, 8))

        self.now_playing = ttk.Label(
            frame, text="Czekam na dane z gry...", font=("Segoe UI", 10, "bold"), wraplength=460
        )
        self.now_playing.pack(pady=6)

        self.match_quality = ttk.Label(frame, text="", foreground="#555555", wraplength=460)
        self.match_quality.pack(anchor="w")

        vol_frame = ttk.Frame(frame)
        vol_frame.pack(fill=tk.X, pady=10)
        ttk.Label(vol_frame, text="Głośność:").pack(side=tk.LEFT)
        ttk.Scale(vol_frame, from_=0, to=1, variable=self.volume).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )

        self.start_btn = ttk.Button(frame, text="▶ Start synchronizacji", command=self.toggle_running)
        self.start_btn.pack(fill=tk.X, pady=8)

        ttk.Label(
            frame,
            text="Wskaż OBS na proces tego programu (gta-radio-music.exe)\nw Application Audio Capture, na osobnym torze audio.",
            foreground="#555555",
        ).pack(anchor="w", pady=(6, 0))

    def choose_status_file(self):
        path = filedialog.askopenfilename(
            title="Wybierz gta_radio_status.json", filetypes=[("JSON", "*.json")]
        )
        if path:
            self.status_file_path.set(path)
            self.status_path_label.config(text=path)
            self._save_config()

    def add_station(self):
        folder = filedialog.askdirectory(title="Wybierz folder stacji (nazwa folderu = ID stacji z gry)")
        if not folder:
            return
        name = os.path.basename(folder.rstrip("/\\"))
        lib = StationLibrary(name, folder)
        if not lib.by_hash:
            messagebox.showwarning("Brak plików", "Nie znaleziono plików .wav w tym folderze.")
            return
        self.stations[name] = lib
        self.station_listbox.insert(tk.END, f"{name} ({len(lib.by_hash)} plików)")
        self._save_config()

    def dump_all_debug(self):
        if not self.stations:
            messagebox.showinfo("Brak stacji", "Najpierw dodaj przynajmniej jedną stację.")
            return
        paths = [lib.dump_debug_hashes() for lib in self.stations.values()]
        messagebox.showinfo("Zapisano", "Zapisano pliki debug:\n" + "\n".join(paths))

    # ---------- Config ----------

    def _save_config(self):
        cfg = {
            "status_file": self.status_file_path.get(),
            "stations": [lib.folder for lib in self.stations.values()],
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return

        status_file = cfg.get("status_file", "")
        if status_file and os.path.exists(status_file):
            self.status_file_path.set(status_file)
            self.status_path_label.config(text=status_file)

        for folder in cfg.get("stations", []):
            if os.path.isdir(folder):
                name = os.path.basename(folder.rstrip("/\\"))
                lib = StationLibrary(name, folder)
                if lib.by_hash:
                    self.stations[name] = lib
                    self.station_listbox.insert(tk.END, f"{name} ({len(lib.by_hash)} plików)")

    # ---------- Audio ----------

    def _audio_callback(self, outdata, frames, time_info, status):
        with self._lock:
            if self._audio_data is None or self._paused_by_game:
                outdata.fill(0)
                return
            remaining = len(self._audio_data) - self._play_pos
            n = min(frames, max(0, remaining))
            vol = self.volume.get()
            if n > 0:
                outdata[:n] = self._audio_data[self._play_pos:self._play_pos + n] * vol
            if n < frames:
                outdata[n:] = 0
            self._play_pos += n

    def _ensure_stream(self, samplerate, channels):
        if self._stream is not None and self._stream.samplerate == samplerate and self._stream.active:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        self._stream = sd.OutputStream(
            samplerate=samplerate, channels=channels, callback=self._audio_callback
        )
        self._stream.start()

    def _play_file(self, path, start_ms=0):
        data, samplerate = sf.read(path, dtype="float32", always_2d=True)
        start_frame = int((start_ms / 1000.0) * samplerate)
        with self._lock:
            self._audio_data = data
            self._samplerate = samplerate
            self._play_pos = min(max(0, start_frame), max(0, len(data) - 1))
            self._paused_by_game = False
        self._ensure_stream(samplerate, data.shape[1])

    # ---------- Synchronizacja ----------

    def toggle_running(self):
        if self.running:
            self.running = False
            self.start_btn.config(text="▶ Start synchronizacji")
            if self._stream is not None:
                self._stream.stop()
        else:
            if not self.status_file_path.get():
                messagebox.showwarning("Brak pliku", "Najpierw wskaż gta_radio_status.json.")
                return
            if not self.stations:
                messagebox.showwarning("Brak stacji", "Dodaj przynajmniej jedną stację.")
                return
            self.running = True
            self.start_btn.config(text="⏸ Stop")
            self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self.poll_thread.start()

    def _poll_loop(self):
        last_station = None
        last_hash = None
        last_playback_ms = None
        last_change_wall = time.time()
        last_in_vehicle = True

        while self.running:
            status = self._read_status()
            if status:
                station = status.get("station", "")
                track_hash = status.get("track_hash", 0)
                playback_ms = status.get("playback_ms", 0)
                in_vehicle = status.get("in_vehicle", True)

                if in_vehicle != last_in_vehicle:
                    last_in_vehicle = in_vehicle
                    if not in_vehicle:
                        self._set_paused(True, reason="Gracz poza pojazdem - radio niesłyszalne")
                    elif track_hash == last_hash:
                        # Wróciliśmy do auta na tym samym utworze - wznów i dosynchronizuj
                        self._set_paused(False)
                        self._correct_drift(playback_ms)

                if not in_vehicle:
                    time.sleep(STATUS_POLL_INTERVAL_SEC)
                    continue

                if track_hash and (station != last_station or track_hash != last_hash):
                    # Nowy utwór (albo nowa stacja)
                    last_station = station
                    last_hash = track_hash
                    last_playback_ms = playback_ms
                    last_change_wall = time.time()
                    self._switch_to(station, track_hash, playback_ms)

                elif not track_hash and station != last_station:
                    last_station = station
                    last_hash = None
                    self._set_paused(True, reason="Radio wyłączone w grze")
                    self.root.after(
                        0, self._update_label,
                        "Radio wyłączone w grze" if not station else f"Stacja: {station}"
                    )

                elif track_hash and track_hash == last_hash:
                    now = time.time()
                    if playback_ms != last_playback_ms:
                        # Gra dalej idzie do przodu - normalna korekta dryfu
                        last_playback_ms = playback_ms
                        last_change_wall = now
                        if self._paused_by_game:
                            self._set_paused(False)
                        self._correct_drift(playback_ms)
                    else:
                        # playback_ms zamrożone - po chwili uznajemy to za pauzę
                        if not self._paused_by_game and (now - last_change_wall) > PAUSE_DETECT_THRESHOLD_SEC:
                            self._set_paused(True, reason="Wykryto pauzę w grze")

            time.sleep(STATUS_POLL_INTERVAL_SEC)

    def _set_paused(self, value, reason=""):
        with self._lock:
            self._paused_by_game = value
        self.root.after(
            0, self._update_quality,
            (reason or "Wyciszono lokalnie") if value else "Wznowiono"
        )

    def _correct_drift(self, playback_ms, threshold_ms=DRIFT_CORRECTION_THRESHOLD_MS):
        with self._lock:
            if self._audio_data is None or self._samplerate is None:
                return
            expected_frame = int((playback_ms / 1000.0) * self._samplerate)
            drift_frames = abs(self._play_pos - expected_frame)
            drift_ms = (drift_frames / self._samplerate) * 1000.0
            if drift_ms > threshold_ms:
                self._play_pos = min(max(0, expected_frame), max(0, len(self._audio_data) - 1))

    def _read_status(self):
        try:
            with open(self.status_file_path.get(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _switch_to(self, station, track_hash, playback_ms=0):
        lib = self.stations.get(station)
        if not lib:
            self.root.after(0, self._update_label, f"Stacja '{station}' - brak dodanego folderu")
            return
        path = lib.find(track_hash)
        if path:
            self._play_file(path, start_ms=playback_ms)
            self.root.after(0, self._update_label, f"[{station}] {os.path.basename(path)}")
            self.root.after(0, self._update_quality, "Dopasowanie OK")
        else:
            fallback = lib.random_file()
            if fallback:
                self._play_file(fallback, start_ms=0)
                self.root.after(0, self._update_label, f"[{station}] {os.path.basename(fallback)} (losowo - brak dopasowania)")
            self.root.after(
                0, self._update_quality,
                f"Brak lokalnego pliku dla hasha {track_hash} w stacji '{station}' - gra losowy utwór"
            )

    def _update_label(self, text):
        self.now_playing.config(text=text)

    def _update_quality(self, text):
        self.match_quality.config(text=text)


def main():
    root = tk.Tk()
    SyncPlayer(root)
    root.mainloop()


if __name__ == "__main__":
    main()