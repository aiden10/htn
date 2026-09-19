# Shutterdex badge bridge

Shutterdex is now two independent badge apps rather than one large Lua app:

- **Pokédex** lives in `/littlefs/apps/pokedex/` and reads the collection
  records. It may optionally use a small number of sprite assets.
- **Habitat** lives in `/littlefs/apps/habitat/` and reads the same world,
  state, motion, event, and dialogue records. It intentionally receives no
  sprite assets, keeping its visual renderer and its filesystem small.

The Web Serial bridge fetches one server snapshot and publishes it to both app
folders. Each app gets `inbox.tmp` first and `inbox.ready` last, so it accepts
only a complete matching revision. A partial publish can therefore leave one
app on the preceding revision, but it cannot make either app read a half-written
snapshot.

## Build the badge imports

Keep the readable sources as the files you edit. Build the minified imports
immediately before importing them into the badge IDE. From
`C:\Users\aiden\Documents\htn\server\shutterdex`, run:

```powershell
py .\minify_lua.py ..\..\badge\pokedex\pokedex-source.lua ..\..\badge\pokedex\pokedex-import.min.lua --verify
py .\minify_lua.py ..\..\badge\habitat\habitat-source.lua ..\..\badge\habitat\habitat-import.min.lua --verify
```

`--verify` makes the command fail if minification changes the Lua token stream.
The minifier preserves the app manifest and string literals; do not hand-edit
the generated `*-import.min.lua` files. Re-run the matching command after every
source edit.

Import `pokedex-import.min.lua` and `habitat-import.min.lua` into the badge
IDE, then Push each app. They are separate launcher entries, so either one can
be opened without allocating the other app's UI.

## Publish a world snapshot

1. Restart FastAPI after applying server changes, if it is not using reload.
2. In `C:\Users\aiden\Documents\htn\server\shutterdex`, run
   `py sprite_bridge.py`.
3. Close the badge IDE tab. Open `http://127.0.0.1:8765` in Chrome or Edge and
   hard-refresh the page.
4. Choose a world, connect the badge, and select **Publish now**. The log should
   show `pokedex/inbox.tmp`, `pokedex/inbox.ready`, `habitat/inbox.tmp`, and
   `habitat/inbox.ready`.
5. Open either Shutterdex app from the badge launcher. **Start live sync** checks
   for a newer server revision once per second. **Advance world** performs one
   intentional server tick, then publishes the result to both apps. Test-world
   advances use the local director for a fast visual demo; the Live world keeps
   the AI director and can take longer.

The bridge sends up to four 28×28 sprite binaries to the Pokédex folder only,
followed by its `sprites.ready` manifest. A sprite is 2,364 bytes, and a missing
asset falls back to text. Habitat deliberately does not receive `sprites.ready`
or sprite binaries; it should render its lightweight world from the shared
snapshot alone.

Each badge app has its own 64 KiB filesystem quota and its own Lua memory limit.
Keeping their source and widget trees separate is what prevents the old combined
app's startup allocation from blocking both features.
