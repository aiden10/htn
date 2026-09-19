--[==[badge-app
slug=pokedex
name=Shutterdex Dex
icon=DEX
api=2
heap_kb=96
]==]

-- Small, file-backed Pokédex.  The bridge commits sprites.ready, inbox.tmp,
-- then inbox.ready; only a matching revision is shown.

local root, cards, marks, names, types, picture
local title, subtitle
local pokemon, sprites = {}, {}
local selected, revision, detail, next_poll = 1, -1, false, 0
local scrollers, next_scroll = {}, 0

local BG, CARD, SELECTED = 0x102018, 0x204431, 0x347f60
local TEXT, MUTED, ACCENT = 0xf1f6ef, 0xb6cfbd, 0x75dca5

local function clip(text, length)
  text = text or ""
  if #text > length then return text:sub(1, length - 2) .. ".." end
  return text
end

local function pretty(text)
  return (text or ""):gsub("_", " ")
end

local function type_colour(element)
  element = (element or ""):lower():match("^[^/ ]+") or ""
  if element == "electric" then return 0xf2cc51 end
  if element == "earth" then return 0xd1a06c end
  if element == "grass" then return 0x79ce70 end
  if element == "fire" then return 0xef7e67 end
  if element == "water" then return 0x70b7ed end
  return ACCENT
end

local function revision_in(text)
  return tonumber((text or ""):match("revision=(%d+)"))
end

local function current_page()
  return math.floor((selected - 1) / 4) * 4
end

local function hide(widget, value)
  widget:hidden(value)
end

local function set_label(widget, x, y, width, height, text, colour)
  widget:set_pos(x, y)
  widget:set_size(width, height)
  widget:set_text(text or "")
  widget:set_color(colour or TEXT)
  hide(widget, false)
end

local function set_card(widget, x, y, width, height, colour)
  widget:set_pos(x, y)
  widget:set_size(width, height)
  widget:set_color(colour)
  hide(widget, false)
end

local function clear_scrollers()
  scrollers, next_scroll = {}, 0
end

-- Badge labels do not have a reliable marquee mode in this small Lua API, so
-- scroll only the focused card/detail fields a character at a time.
local function set_scroller(widget, x, y, width, height, text, length, colour)
  text = pretty(text)
  set_label(widget, x, y, width, height, text, colour)
  if #text <= length then return end
  local gap = "    "
  local loop = #text + #gap
  local stream = text .. gap .. text
  scrollers[widget] = {stream = stream, loop = loop, length = length, offset = 1}
  widget:set_text(stream:sub(1, length))
end

local function tick_scrollers(now)
  if now < next_scroll then return end
  next_scroll = now + 250
  for widget, scroll in pairs(scrollers) do
    scroll.offset = scroll.offset + 1
    if scroll.offset > scroll.loop then scroll.offset = 1 end
    widget:set_text(scroll.stream:sub(scroll.offset, scroll.offset + scroll.length - 1))
  end
end

local function hide_cells(from)
  for index = from or 1, 4 do
    hide(cards[index], true)
    hide(marks[index], true)
    hide(names[index], true)
    hide(types[index], true)
  end
end

local function hide_picture()
  if picture then hide(picture, true) end
end

local function read_sprites(expected_revision)
  local text = badge.fs.read("sprites.ready") or ""
  if revision_in(text) ~= expected_revision then return {} end
  local available = {}
  for line in text:gmatch("[^\r\n]+") do
    local key = line:match("^sprite|([^|\r\n]+)")
    if key then available[key] = true end
  end
  return available
end

local function same_sprites(first, second)
  for key in pairs(first) do
    if not second[key] then return false end
  end
  for key in pairs(second) do
    if not first[key] then return false end
  end
  return true
end

local function read_world()
  local wanted = revision_in(badge.fs.read("inbox.ready"))
  if not wanted then return false end

  -- Sprite files may finish arriving after a snapshot with this same revision
  -- was already accepted.  A forced bridge publish intentionally keeps the
  -- world revision unchanged, so refresh the small asset manifest too.
  if wanted == revision then
    local refreshed_sprites = read_sprites(wanted)
    if same_sprites(sprites, refreshed_sprites) then return false end
    sprites = refreshed_sprites
    return true
  end

  local text = badge.fs.read("inbox.tmp") or ""
  if revision_in(text) ~= wanted then return false end
  if revision_in(badge.fs.read("inbox.ready")) ~= wanted then return false end

  local fresh = {}
  for line in text:gmatch("[^\r\n]+") do
    local id, name, caught, species, element, hp, attack, defense, speed, rarity, flavour, sprite =
      line:match("^pokemon|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$")
    if id and name and #fresh < 16 then
      fresh[#fresh + 1] = {id, name, caught, species, element, hp, attack, defense, speed, rarity, flavour, sprite}
    end
  end

  pokemon, sprites, revision = fresh, read_sprites(wanted), wanted
  if selected > #pokemon then selected = #pokemon end
  if selected < 1 then selected = 1 end
  return true
end

local function show_picture(monster, x, y, size)
  hide_picture()
  local key = monster and monster[12]
  if not key or not sprites[key] then return false end
  if not picture then picture = badge.ui.image(root, key .. ".bin") else picture:set_src(key .. ".bin") end
  picture:set_pos(x, y)
  picture:set_size(size or 28, size or 28)
  hide(picture, false)
  picture:bring_to_front()
  return true
end

local function draw_grid()
  detail = false
  clear_scrollers()
  hide_picture()
  set_label(title, 8, 7, 304, 21, "POKEDEX", ACCENT)
  set_label(subtitle, 8, 28, 304, 14, #pokemon .. " CAPTURED - PAGE " .. (math.floor(current_page() / 4) + 1), MUTED)

  local page = current_page()
  for cell = 1, 4 do
    local index = page + cell
    local monster = pokemon[index]
    if monster then
      local column = (cell - 1) % 2
      local row = math.floor((cell - 1) / 2)
      local x, y = 8 + column * 156, 49 + row * 82
      set_card(cards[cell], x, y, 148, 73, index == selected and SELECTED or CARD)
      local image_shown = index == selected and show_picture(monster, x + 6, y + 7, 34)
      if image_shown then
        hide(marks[cell], true)
      else
        set_label(marks[cell], x + 8, y + 10, 31, 20, monster[2]:sub(1, 2):upper(), type_colour(monster[5]))
      end
      if index == selected then
        set_scroller(names[cell], x + 46, y + 8, 94, 17, monster[2], 12, TEXT)
        set_scroller(types[cell], x + 46, y + 28, 94, 17, monster[5]:upper(), 13, type_colour(monster[5]))
      else
        set_label(names[cell], x + 46, y + 8, 94, 17, clip(monster[2], 12), TEXT)
        local element = pretty(monster[5]):upper():gsub("/", "\n")
        set_label(types[cell], x + 46, y + 28, 94, 31, element, type_colour(monster[5]))
      end
    else
      hide(cards[cell], true); hide(marks[cell], true); hide(names[cell], true); hide(types[cell], true)
    end
  end

  if #pokemon == 0 then
    set_label(names[1], 18, 82, 284, 20, "No Pokemon received yet.", TEXT)
    set_label(types[1], 18, 106, 284, 36, "Connect the bridge, then publish a snapshot.", MUTED)
  end
end

local function draw_detail()
  clear_scrollers()
  hide_cells(2)
  hide_picture()
  set_label(title, 8, 7, 304, 21, "POKEDEX", ACCENT)
  set_label(subtitle, 8, 28, 304, 14, "DETAILS", MUTED)

  local monster = pokemon[selected]
  if not monster then
    set_card(cards[1], 8, 49, 304, 183, CARD)
    set_label(names[1], 20, 82, 272, 20, "No Pokemon received yet.", TEXT)
    set_label(types[1], 20, 108, 272, 20, "Return after publishing a snapshot.", MUTED)
    hide(marks[1], true)
    return
  end

  set_card(cards[1], 8, 49, 304, 183, CARD)
  local image_shown = show_picture(monster, 18, 57, 56)
  if image_shown then
    hide(marks[1], true)
  else
    set_label(marks[1], 18, 75, 56, 20, monster[2]:sub(1, 2):upper(), type_colour(monster[5]))
  end
  set_scroller(names[1], 84, 58, 214, 19, monster[2], 22, TEXT)
  set_scroller(types[1], 84, 79, 214, 17, monster[5]:upper(), 25, type_colour(monster[5]))
  set_label(names[2], 18, 116, 90, 14, "SPECIES", ACCENT)
  set_scroller(types[2], 18, 130, 280, 16, monster[4], 39, MUTED)
  set_label(names[3], 18, 149, 280, 16, "HP " .. monster[6] .. "  ATK " .. monster[7] .. "  DEF " .. monster[8] .. "  SPD " .. monster[9], TEXT)
  set_label(names[4], 18, 168, 280, 14, "CAUGHT " .. monster[3] .. "  " .. monster[10]:upper(), MUTED)
  set_label(types[3], 18, 185, 280, 14, "NOTE", ACCENT)
  set_scroller(types[4], 18, 199, 280, 24, monster[11], 39, TEXT)
end

local function draw()
  if detail then draw_detail() else draw_grid() end
end

function on_enter(app_root)
  root = app_root
  local background = badge.ui.box(root, 320, 240)
  background:set_pos(0, 0)
  background:style({bg_color = BG, border_width = 0})

  cards, marks, names, types = {}, {}, {}, {}
  for index = 1, 4 do
    cards[index] = badge.ui.box(root, 1, 1)
    cards[index]:style({bg_color = CARD, border_color = 0x64957a, border_width = 1, radius = 4})
    marks[index] = badge.ui.label(root, "")
    names[index] = badge.ui.label(root, "")
    types[index] = badge.ui.label(root, "")
    marks[index]:style({text_font = 14, text_color = ACCENT})
    names[index]:style({text_font = 14, text_color = TEXT})
    types[index]:style({text_font = 14, text_color = MUTED})
  end
  title = badge.ui.label(root, "")
  subtitle = badge.ui.label(root, "")
  title:style({text_font = 14, text_color = ACCENT})
  subtitle:style({text_font = 14, text_color = MUTED})

  read_world()
  draw_grid()
end

function on_tick()
  local now = badge.sys.ms()
  if now >= next_poll then
    next_poll = now + 900
    if read_world() then draw() end
  end
  tick_scrollers(now)
end

function on_button(button, kind)
  if kind ~= badge.input.KIND.PRESSED then return end
  local key, count = badge.input.BUTTON, #pokemon
  if detail then
    if button == key.A or button == key.B then detail = false
    elseif count > 0 and (button == key.LEFT or button == key.UP) then selected = (selected - 2) % count + 1
    elseif count > 0 and (button == key.RIGHT or button == key.DOWN) then selected = selected % count + 1 end
  elseif button == key.A and count > 0 then
    detail = true
  elseif count > 0 then
    if button == key.LEFT and (selected - 1) % 2 == 1 then selected = selected - 1 end
    if button == key.RIGHT and (selected - 1) % 2 == 0 and selected < count then selected = selected + 1 end
    if button == key.UP and selected > 2 then selected = selected - 2 end
    if button == key.DOWN and selected + 2 <= count then selected = selected + 2
    elseif button == key.DOWN and selected < count then selected = count end
  end
  draw()
end

function on_exit() end
