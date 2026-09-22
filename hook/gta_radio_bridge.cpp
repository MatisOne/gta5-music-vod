// gta_radio_bridge.cpp
// ============================================================================
// Plugin ScriptHookV (kompilowany jako .asi) monitorujący stan radia w GTA V
// i zapisujący go do pliku, z którego czyta go zewnętrzny odtwarzacz
// (gta_radio_music_v4.py / gta-radio-music.exe).
//
// NIE modyfikuje rozgrywki, nie czyta/zapisuje niczego poza odczytem stanu
// audio przez oficjalne natywne funkcje gry (te same, których używają
// dziesiątki publicznych modów SP, np. trainery pokazujące nazwę stacji).
//
// v2: zamiast zgadywać utwór po długości nagrania, wysyłamy dokładny HASH
// (joaat) aktualnie granego dźwięku z AUDIO::GET_CURRENT_TRACK_SOUND_NAME.
// Odtwarzacz po stronie Pythona liczy joaat z nazw lokalnych plików
// (wyeksportowanych przez OpenIV, z zachowanymi oryginalnymi nazwami) i
// dopasowuje 1:1 po hashu - żadnego zgadywania.
//
// WYMAGANIA DO ZBUDOWANIA: Visual Studio Build Tools + oficjalne ScriptHookV
// SDK (dev-c.com), projekt jako DLL linkowany z ScriptHookV.lib, wynik
// zmieniony na rozszerzenie .asi, wrzucony do folderu gry razem z ASI
// Loaderem (dinput8.dll).
// ============================================================================

#include "main.h"
#include "natives.h"

#include <Windows.h>
#include <fstream>
#include <string>
#include <sstream>

// Ścieżka pliku statusu - zapisywany w katalogu gry (obok GTA5.exe)
static const char* STATUS_FILE = "gta_radio_status.json";

static void write_status(const std::string& station, unsigned int trackHash, int playbackMs, bool inVehicle)
{
    std::ofstream out(STATUS_FILE, std::ios::trunc);
    if (!out.is_open())
        return;

    std::ostringstream json;
    json << "{"
         << "\"station\":\"" << station << "\","
         << "\"track_hash\":" << trackHash << ","
         << "\"playback_ms\":" << playbackMs << ","
         << "\"in_vehicle\":" << (inVehicle ? "true" : "false")
         << "}";

    out << json.str();
    out.close();
}

static void update_tick()
{
    // Czy gracz jest w pojeździe - radio w GTA V faktycznie gra tylko wtedy
    // (poza autem stacja "trwa" wybrana w tle, ale nie jest słyszalna).
    Ped playerPed = PLAYER::PLAYER_PED_ID();
    bool inVehicle = PED::IS_PED_IN_ANY_VEHICLE(playerPed, FALSE);

    // Nazwa aktualnej stacji (np. "RADIO_01_CLASS_ROCK"), pusta jeśli radio wyłączone
    const char* stationNamePtr = AUDIO::GET_PLAYER_RADIO_STATION_NAME();
    std::string station = stationNamePtr ? std::string(stationNamePtr) : std::string("");

    unsigned int trackHash = 0;
    int playbackMs = 0;
    if (!station.empty())
    {
        // Wrapper w natives.h ma zły typ zwracany (char* zamiast Hash/uint32),
        // więc wołamy natywną bezpośrednio po hashu z poprawnym typem:
        trackHash = invoke<unsigned int>(0x34D66BC058019CE0, (char*)station.c_str());

        // Dokładna pozycja (ms) w aktualnie granym utworze - tym samym stylem invoke<>,
        // żeby odtwarzacz mógł zsynchronizować się nie tylko co do utworu, ale i co
        // do konkretnego momentu w nim (ważne np. przy starcie programu w trakcie utworu).
        playbackMs = invoke<int>(0x3E65CDE5215832C1, (char*)station.c_str());
    }

    write_status(station, trackHash, playbackMs, inVehicle);
}

void ScriptMain()
{
    while (true)
    {
        update_tick();
        WAIT(150);
    }
}

BOOL WINAPI DllMain(HINSTANCE hInstance, DWORD reason, LPVOID lpReserved)
{
    switch (reason)
    {
    case DLL_PROCESS_ATTACH:
        scriptRegister(hInstance, ScriptMain);
        break;
    case DLL_PROCESS_DETACH:
        scriptUnregister(hInstance);
        break;
    }
    return TRUE;
}