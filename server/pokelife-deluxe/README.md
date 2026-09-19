# PokeLife deluxe assets

This package adds a visual PokeLife app and a local Web Serial publisher. It
does not replace the FastAPI server: leave that running on port 8000 as before.

1. Import `pokedex-deluxe-import.lua` into the badge IDE, then **Push +
   Reboot** so its 96 KiB Lua heap setting takes effect.
2. In this folder, run `py sprite_bridge.py`.
3. Close the badge IDE tab, open `http://127.0.0.1:8765` in Chrome or Edge,
   choose the world, connect the badge, and select **Publish now**.
4. Launch PokeLife. Select **Start live sync** if the server will keep making
   simulation updates while the badge remains plugged in.

The bridge converts the bundled transparent PNG art to the badge's RGB565A8
format at 40×40 pixels. It sends each sprite before `inbox.tmp` and
`inbox.ready`; for future Pokémon it also proxies a matching binary from the
server's existing `/sprites/{key}.bin` endpoint. An unknown sprite keeps the
app usable with its text fallback. The test world has Coilkit and Mossbyte art.
The live demo world has Mugmite and Spriglet art.

The four binary sprite files total about 19 KiB on the badge (4,812 bytes each),
leaving room under the normal per-app storage quota for the Lua app and inbox.
