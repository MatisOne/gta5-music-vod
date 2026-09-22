# GTA V - External Music Player
This app plays GTA V radio music separately from the game. Then, in OBS Studio, you can mute the music on a Twitch VOD or YouTube stream (or for recording).

## How it works?
This mod uses ScriptHook to check what's currently playing on the in-game radio. Simply mute the audio by changing the music volume to zero in the game settings. The game is still playing music internally, even when it is completely muted. The game changes tracks and radio stations without any issues. Then, this app just plays music as a separate app, so you can output it to a separate device. You can capture it using the OBS application audio capture feature.

## How to use it?
Place the ``GTA_Radio_Bridge.asi`` file in your Grand Theft Auto V Legacy or Enhanced main folder. Make sure you already have the ScriptHook file here.
If done properly, this mod will create the file ``gta_radio_status.json`` which contains all information about the radio station you listen to in the game.
If the file does not exist, check your Windows file permissions. You can also create the file yourself and fix the Windows permissions. Make sure it is not read-only.
Use ``gta-radio-music.exe`` to play music.

## How to build it?
Build a Hook
cl /LD /EHsc /I inc gta_radio_bridge.cpp ScriptHookV.lib /Fe:gta_radio_bridge.asi

Build a App
pyinstaller --onefile --windowed --name gta-radio-music gta_radio_music.py
