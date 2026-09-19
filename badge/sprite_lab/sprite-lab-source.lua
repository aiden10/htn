--[==[badge-app
slug=sprite_lab
name=Shutterdex Sprite Lab
icon=IMG
api=2
heap_kb=48
]==]

-- Purpose-built image-decoder diagnostic.  This app never substitutes text
-- for a present image: if the runtime cannot decode a selected .bin asset,
-- its normal Lua error card exposes that failure immediately.

local root, image
local title, format_label, file_label, status, hint, frame
local selected, next_probe, last_available = 1, 0, nil

local samples = {
  {file = "rgb565a8_42.bin", label = "42 x 42  (IDE canonical)", size = 42},
  {file = "rgb565a8_32.bin", label = "32 x 32  (server sprite)", size = 32},
  {file = "rgb565a8_28.bin", label = "28 x 28  (bridge sprite)", size = 28},
}

local C = {bg = 0x0d1912, panel = 0x193425, border = 0x608a70, text = 0xf0f7ef, muted = 0xa9c3ae, accent = 0x55d494}

local function label(parent, x, y, w, h, text, style)
  local item = badge.ui.label(parent, text or "")
  item:set_pos(x, y)
  item:set_size(w, h)
  item:style(style)
  return item
end

local function draw(force)
  local sample = samples[selected]
  -- `read` is binary-safe and is the exact access route the Pokedex uses for
  -- its inbox files.  It also gives the diagnostic an unambiguous byte count;
  -- some firmware builds report `exists` only for private appdata paths.
  local bytes = badge.fs.read(sample.file)
  local available = bytes ~= nil and #bytes >= 12
  local byte_count = bytes and #bytes or 0
  if not force and available == last_available then return end
  last_available = available
  file_label:set_text(sample.label)
  if not available then
    status:set_text("ASSET MISSING - run sprite_lab_assets.py")
    image = image or badge.ui.box(root, 1, 1)
    image:hidden(true)
    return
  end

  -- No pcall and no text fallback here. A corrupt or unsupported .bin causes
  -- the badge's own Lua error screen, which is the useful diagnostic result.
  if image and image:type() == "image" then
    image:set_src(sample.file)
  else
    if image then image:delete() end
    image = badge.ui.image(root, sample.file)
  end
  image:set_size(sample.size, sample.size)
  image:set_pos(160 - math.floor(sample.size / 2), 112 - math.floor(sample.size / 2))
  image:hidden(false)
  image:bring_to_front()
  status:set_text("READ " .. byte_count .. " B - decoder invoked")
end

function on_enter(parent)
  root = parent
  local background = badge.ui.box(root, 320, 240)
  background:set_pos(0, 0)
  background:style({bg_color = C.bg, border_width = 0})
  title = label(root, 12, 12, 296, 24, "SPRITE LAB", {text_font = 20, text_color = C.text, text_align = "center"})
  format_label = label(root, 12, 40, 296, 16, "LVGL RGB565A8 .bin diagnostics", {text_font = 14, text_color = C.muted, text_align = "center"})
  frame = badge.ui.box(root, 112, 112)
  frame:set_pos(104, 58)
  frame:style({bg_color = C.panel, border_color = C.border, border_width = 2, radius = 8})
  file_label = label(root, 12, 177, 296, 18, "", {text_font = 14, text_color = C.accent, text_align = "center"})
  status = label(root, 12, 198, 296, 18, "", {text_font = 14, text_color = C.text, text_align = "center"})
  hint = label(root, 12, 222, 296, 14, "A next sample   B refresh", {text_font = 14, text_color = C.muted, text_align = "center"})
  draw(true)
end

function on_tick()
  local now = badge.sys.ms()
  if now >= next_probe then
    next_probe = now + 700
    draw(false)
  end
end

function on_button(button, kind)
  if kind ~= badge.input.KIND.PRESSED then return end
  if button == badge.input.BUTTON.A then
    selected = selected % #samples + 1
    last_available = nil
    draw(true)
  elseif button == badge.input.BUTTON.B then
    last_available = nil
    draw(true)
  end
end

function on_exit() end
