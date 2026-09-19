--[==[badge-app
slug=pokedex
name=Pocket Pokedex
icon=DEX
api=2
heap_kb=48
wake_lock=1
]==]

-- Pocket Pokedex
--
-- The companion bridge writes inbox.tmp completely, then writes inbox.ready
-- with the same revision number. This app ignores partial/mismatched updates.
--
-- Controls: D-pad selects a Pokemon. HOME returns to the launcher.

local GRID_COLUMNS = 2
local GRID_ROWS = 4
local GRID_SIZE = GRID_COLUMNS * GRID_ROWS
local GRID_X = 5
local GRID_Y = 38
local CELL_W = 72
local CELL_H = 38
local CELL_GAP_X = 4
local CELL_GAP_Y = 5
local DETAIL_X = 162

local roster = {}
local selected = 1
local loaded_revision = -1
local next_inbox_check = 0

local grid_boxes = {}
local grid_labels = {}
local count_label
local status_label
local detail_name
local detail_date
local detail_classification
local detail_types
local detail_sprite

local function shorten(text, limit)
  if #text <= limit then return text end
  return string.sub(text, 1, limit - 1) .. "."
end

local function type_color(types)
  if string.find(types, "Fire", 1, true) then return 0xe85d45 end
  if string.find(types, "Water", 1, true) then return 0x4d9be6 end
  if string.find(types, "Electric", 1, true) then return 0xf2c94c end
  if string.find(types, "Grass", 1, true) then return 0x64b864 end
  if string.find(types, "Rock", 1, true) then return 0xa98f73 end
  if string.find(types, "Bug", 1, true) then return 0x91b84a end
  if string.find(types, "Fairy", 1, true) then return 0xd786c5 end
  return 0x7f8c9b
end

local function show_type_leds(types)
  local color = type_color(types)
  local red = math.floor(color / 65536)
  local green = math.floor((color % 65536) / 256)
  local blue = color % 256
  badge.led.set_all(red, green, blue)
  badge.led.show()
end

local function revision_from(text)
  if not text then return nil end
  return tonumber(string.match(text, "revision=(%d+)"))
end

local function parse_inbox(text, expected_revision)
  if revision_from(text) ~= expected_revision then return nil end

  local parsed = {}
  for line in string.gmatch(text, "[^\r\n]+") do
    local name, caught, classification, types = string.match(
      line,
      "^pokemon|([^|]*)|([^|]*)|([^|]*)|([^|]*)$"
    )
    if name and name ~= "" and caught ~= "" and classification ~= "" and types ~= "" then
      parsed[#parsed + 1] = {
        name = name,
        caught = caught,
        classification = classification,
        types = types,
      }
    end
  end
  return parsed
end

local function page_start()
  return math.floor((selected - 1) / GRID_SIZE) * GRID_SIZE + 1
end

local function render_grid()
  local start = page_start()
  for slot = 1, GRID_SIZE do
    local index = start + slot - 1
    local box = grid_boxes[slot]
    local label = grid_labels[slot]
    local pokemon = roster[index]

    if pokemon then
      box:hidden(false)
      label:hidden(false)
      if index == selected then
        box:set_color(type_color(pokemon.types))
        box:set_border(0xffffff, 2)
      else
        box:set_color(0x242b36)
        box:set_border(0x46515f, 1)
      end
      label:set_text(shorten(pokemon.name, 10) .. "\n" .. shorten(pokemon.types, 10))
    else
      box:hidden(true)
      label:hidden(true)
    end
  end
end

local function render_details()
  local pokemon = roster[selected]
  if not pokemon then
    detail_name:set_text("No Pokemon")
    detail_date:set_text("Waiting for inbox")
    detail_classification:set_text("")
    detail_types:set_text("")
    detail_sprite:set_color(0x46515f)
    badge.led.clear()
    badge.led.show()
    return
  end

  detail_name:set_text(shorten(pokemon.name, 18))
  detail_date:set_text("Caught: " .. shorten(pokemon.caught, 16))
  detail_classification:set_text(shorten(pokemon.classification, 20))
  detail_types:set_text("Type: " .. shorten(pokemon.types, 18))
  detail_sprite:set_color(type_color(pokemon.types))
  show_type_leds(pokemon.types)
end

local function render_all()
  count_label:set_text("POKEDEX  " .. #roster)
  render_grid()
  render_details()
end

local function load_inbox()
  local ready_revision = revision_from(badge.fs.read("inbox.ready"))
  if not ready_revision or ready_revision == loaded_revision then return end

  local parsed = parse_inbox(badge.fs.read("inbox.tmp"), ready_revision)
  if not parsed then
    status_label:set_text("Inbox updating...")
    return
  end

  roster = parsed
  loaded_revision = ready_revision
  if #roster == 0 then
    selected = 1
  elseif selected > #roster then
    selected = #roster
  end
  status_label:set_text("Inbox rev " .. ready_revision)
  render_all()
end

local function move_selection(horizontal, vertical)
  if #roster == 0 then return end

  local target = selected
  if horizontal < 0 then
    if (selected - 1) % GRID_COLUMNS == 1 then target = selected - 1 end
  elseif horizontal > 0 then
    if (selected - 1) % GRID_COLUMNS == 0 and selected + 1 <= #roster then
      target = selected + 1
    end
  elseif vertical < 0 and selected > GRID_COLUMNS then
    target = selected - GRID_COLUMNS
  elseif vertical > 0 and selected + GRID_COLUMNS <= #roster then
    target = selected + GRID_COLUMNS
  end

  if target ~= selected then
    selected = target
    render_all()
  end
end

function on_enter(root)
  local background = badge.ui.box(root, 320, 240)
  background:set_pos(0, 0)
  background:style({bg_color = 0x11161e})

  local grid_panel = badge.ui.box(root, 156, 228)
  grid_panel:set_pos(2, 6)
  grid_panel:style({bg_color = 0x171e29, border_color = 0x384454, border_width = 1, radius = 5})

  local detail_panel = badge.ui.box(root, 154, 228)
  detail_panel:set_pos(DETAIL_X, 6)
  detail_panel:style({bg_color = 0x1b2330, border_color = 0x526378, border_width = 1, radius = 5})

  count_label = badge.ui.label(root, "POKEDEX")
  count_label:set_pos(8, 14)
  count_label:style({text_font = 16, text_color = 0xffffff})

  status_label = badge.ui.label(root, "Loading inbox...")
  status_label:set_pos(8, 216)
  status_label:style({text_font = 14, text_color = 0xaebdce})

  for slot = 1, GRID_SIZE do
    local column = (slot - 1) % GRID_COLUMNS
    local row = math.floor((slot - 1) / GRID_COLUMNS)
    local x = GRID_X + column * (CELL_W + CELL_GAP_X)
    local y = GRID_Y + row * (CELL_H + CELL_GAP_Y)

    local cell = badge.ui.box(root, CELL_W, CELL_H)
    cell:set_pos(x, y)
    cell:style({bg_color = 0x242b36, border_color = 0x46515f, border_width = 1, radius = 4})
    grid_boxes[slot] = cell

    local cell_label = badge.ui.label(cell, "")
    cell_label:set_pos(4, 4)
    cell_label:style({text_font = 14, text_color = 0xffffff})
    grid_labels[slot] = cell_label
  end

  local detail_title = badge.ui.label(root, "SELECTED")
  detail_title:set_pos(DETAIL_X + 8, 15)
  detail_title:style({text_font = 14, text_color = 0xaebdce})

  -- This is deliberately a UI box rather than an image resource: the current
  -- IDE importer accepts text only, so a binary sprite cannot be imported.
  detail_sprite = badge.ui.box(root, 32, 32)
  detail_sprite:set_pos(DETAIL_X + 106, 31)
  detail_sprite:style({bg_color = 0x46515f, border_color = 0xffffff,
                       border_width = 1, radius = 5})

  detail_name = badge.ui.label(root, "")
  detail_name:set_pos(DETAIL_X + 8, 48)
  detail_name:style({text_font = 20, text_color = 0xffffff})

  detail_date = badge.ui.label(root, "")
  detail_date:set_pos(DETAIL_X + 8, 82)
  detail_date:style({text_font = 14, text_color = 0xaebdce})

  detail_classification = badge.ui.label(root, "")
  detail_classification:set_pos(DETAIL_X + 8, 110)
  detail_classification:style({text_font = 16, text_color = 0xffffff})

  detail_types = badge.ui.label(root, "")
  detail_types:set_pos(DETAIL_X + 8, 142)
  detail_types:style({text_font = 16, text_color = 0xaebdce})

  local detail_hint = badge.ui.label(root, "D-pad: select\nHOME: exit")
  detail_hint:set_pos(DETAIL_X + 8, 194)
  detail_hint:style({text_font = 14, text_color = 0x8da0b5})

  load_inbox()
  if loaded_revision < 0 then render_all() end
end

function on_tick()
  local now = badge.sys.ms()
  if now < next_inbox_check then return end
  next_inbox_check = now + 1000
  load_inbox()
end

function on_button(button, kind)
  if kind ~= badge.input.KIND.PRESSED then return end
  local B = badge.input.BUTTON
  if button == B.LEFT then move_selection(-1, 0)
  elseif button == B.RIGHT then move_selection(1, 0)
  elseif button == B.UP then move_selection(0, -1)
  elseif button == B.DOWN then move_selection(0, 1)
  end
end

function on_exit()
  badge.led.clear()
  badge.led.show()
end

