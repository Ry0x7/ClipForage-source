# Editor integrations

clipforage downloads into one library folder (`[library] dir` in `config.toml`, default
`~/Downloads/clipforage`). Each media file has a `<file>.json` sidecar with its license,
attribution and source URL. This page explains how to get that folder into your editor.

## DaVinci Resolve

You can import the library into the media pool of the open project in two ways:

- `clipforage resolve-import`: run it from a terminal while Resolve is open.
- `scripts/resolve/clipforage_import.py`: a standalone script you run from Resolve's
  Workspace > Scripts menu. It does not need clipforage installed.

Both put the files in a bin called `clipforage` under the media pool root, creating the bin
if it does not exist.

### Prerequisites

- DaVinci Resolve must be running and fully loaded, with a project open. The command does
  not create or switch projects.
- `clipforage resolve-import` connects to Resolve from outside the app. Turn this on in
  Preferences > System > General > "External scripting using" = **Local**. Resolve's
  scripting README documents this setting for DaVinci Resolve Studio. Whether the free
  version offers it was not verified (only Studio 21.1 was tested). The in-app script
  works without it.
- clipforage finds Resolve's scripting module (`DaVinciResolveScript` and `fusionscript`)
  in the default install locations given in Resolve's README:

  | OS | `RESOLVE_SCRIPT_API` | `RESOLVE_SCRIPT_LIB` |
  | --- | --- | --- |
  | Windows | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` | `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll` |
  | macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting` | `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so` |
  | Linux | `/opt/resolve/Developer/Scripting` | `/opt/resolve/libs/Fusion/fusionscript.so` |

  If you set these environment variables yourself, clipforage uses your values. If a
  variable is unset and the default path exists, clipforage fills it in. clipforage also
  adds `<RESOLVE_SCRIPT_API>/Modules` to the Python path for you. If Resolve is installed
  somewhere else, set `RESOLVE_SCRIPT_LIB`. For example, on Windows with Resolve on drive E:

  ```powershell
  $env:RESOLVE_SCRIPT_LIB = "E:\davinci\fusionscript.dll"
  ```

### `clipforage resolve-import`

```sh
clipforage resolve-import                       # import new files from the library folder
clipforage resolve-import --library D:\clips    # import from a different folder
clipforage resolve-import --bin "memes"         # import into a different bin
clipforage resolve-import --all                 # import again even if the file is already in the bin
clipforage resolve-import --recursive           # include files in subfolders
```

The command prints how many files were imported, skipped (already in the bin) or failed,
followed by the file names. It exits 1 with a message in three cases: Resolve cannot be
reached (not installed, not running, or external scripting is off), no project is open, or
the library folder does not exist. Failed files do not change the exit code. They are listed
under `failed:`.

What gets imported:

- Every file directly in the library folder, or in subfolders too with `--recursive`.
- Skipped: `*.json` sidecars, partial downloads (`.part`, `.ytdl`, `.tmp`), and hidden
  files (dot-files, or files with the Windows hidden attribute).
- By default (`--only-new`), a file is skipped if a clip in the target bin already has the
  same file path. The check only looks at that bin, so a clip you moved to another bin gets
  imported again.
- All files go to Resolve in a single `ImportMedia` call. A file counts as failed if Resolve
  returns no clip for it, for example because the format is unsupported.

### Running the script inside Resolve

Copy `scripts/resolve/clipforage_import.py` into a `Utility` subfolder of Resolve's Scripts
folder. Restart Resolve, then run **Workspace > Scripts > clipforage_import**. Scripts
folders by OS:

| OS | Current user | All users |
| --- | --- | --- |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility` | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility` |
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility` | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility` | `/opt/resolve/Fusion/Scripts/Utility` |

The script reads `[library] dir` from clipforage's `config.toml`
(`%APPDATA%\clipforage\config.toml` or `~/.config/clipforage/config.toml`). If that is not
set, it uses `~/Downloads/clipforage`. It always imports new files only, into the
`clipforage` bin. It does not search subfolders. Results go to the Resolve console
(Workspace > Console).

### Metadata written

If a clip has a sidecar, clipforage sets its **Comments** metadata to:

```text
<license> | <attribution_text> | <source_url>
```

For example: `CC BY-SA 4.0 | BalukuBrian / Wikimedia Commons, CC BY-SA 4.0 | https://commons.wikimedia.org/wiki/File:Bushenyi_rain.webm`.
If Resolve rejects Comments, clipforage writes the text to **Description** instead. If
writing metadata fails, the import still succeeds. Files without a sidecar get no metadata.

### Limitations

- clipforage needs a running Resolve and an open project. It does not support headless or
  network scripting.
- Metadata is written only once, at import. Re-running the command does not update the
  metadata of clips already in the bin.
- Skipping compares exact file paths. Moving or renaming the library folder makes every
  file look new.
- Deleting a file from the library does not remove its clip from Resolve.
- `ImportMedia`: Resolve 21.1 documents passing a list of `{"FilePath": ...}` dicts as the
  preferred form and plain path strings as deprecated. On 21.1.0.14 the opposite was
  observed: plain paths imported and the dict form returned `None`. clipforage sends plain
  paths first and retries with dicts only if nothing was imported.
- Tested with DaVinci Resolve Studio 21.1.0.14 on Windows only. macOS, Linux and the free
  version were not tested.

## Transparent `.key.webm` files (keyout)

`clipforage keyout <history-id|file>` (or **Key out** in the web UI History tab) keys out a
green screen and writes `<name>.key.webm` next to the source in the library folder: VP9 video
with an alpha channel (`yuva420p`), Opus audio, and a sidecar that copies the source's license
and adds `derived_from` and the `chroma` settings. Because it lands in the library folder,
folder import sees it and `clipforage resolve-import` passes it to Resolve like any other file
(whether Resolve accepts it is part of the caveat below).

**Not verified:** importing `.key.webm` with its transparency into Premiere Pro, DaVinci Resolve
or CapCut has not been tested. Editor support for VP9 with alpha varies, and a decoder that
ignores the alpha plane shows the keyed areas as black or opaque. If that happens:

- Check that the file really has alpha: `ffmpeg -c:v libvpx-vp9 -i name.key.webm` reports
  `yuva420p`. ffmpeg's built-in VP9 decoder (used when you leave out `-c:v libvpx-vp9`) reports
  `yuv420p` with `alpha_mode: 1` and drops the alpha, and some players behave the same way.
- Or import the original green-screen file and use the editor's own chroma keyer. The
  tolerance you picked in the web UI preview only applies to ClipForage's export.

ClipForage does not export other alpha formats (such as ProRes 4444 `.mov`) yet.

## Premiere Pro

There is no Premiere Pro integration yet. A UXP panel was researched and the decision is
GO-LATER: dragging from a third-party panel into the timeline needs Premiere Pro 27, and
loopback/CORS access from a panel to the local server is unconfirmed. See
[PREMIERE_UXP_SPIKE.md](PREMIERE_UXP_SPIKE.md).

Meanwhile:

- Import the library folder (File > Import, or the Media Browser panel). The folder is flat,
  so new downloads appear there directly.
- Or drag a saved file out of the clipforage web UI (Chrome/Edge). The drop target gets a copy
  of the file. Dropping straight into the Premiere timeline has not been verified yet.
