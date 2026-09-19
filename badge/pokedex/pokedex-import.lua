--[==[badge-app
slug=pokedex
name=PokeLife
icon=DEX
api=2
heap_kb=96
wake_lock=1
]==]

-- One app, one revisioned inbox. The bridge writes inbox.tmp first and writes
-- inbox.ready last. The app only swaps to a world when both revisions agree.

local root
local mode = "menu"
local widgets = {}
local roster, states, events = {}, {}, {}
local loaded_revision = -1
local next_inbox_check = 0
local menu_selected, pokemon_selected, habitat_selected = 1, 1, 1

local menu_boxes, menu_status = {}, nil
local grid_boxes, grid_labels = {}, {}
local dex_count, dex_status, dex_name, dex_caught, dex_species, dex_type
local dex_stats_a, dex_stats_b, dex_rarity, dex_avatar
local habitat_actors, habitat_names = {}, {}
local habitat_title, habitat_detail, habitat_event, habitat_talk
local activity_title, activity_lines = nil, {}

local MODES = {"POKEDEX", "HABITAT", "ACTIVITY"}

local function track(widget)
  widgets[#widgets + 1] = widget
  return widget
end

local function clear_view()
  for index = #widgets, 1, -1 do
    widgets[index]:delete()
  end
  widgets = {}
end

local function clamp(value, low, high)
  if value < low then return low end
  if value > high then return high end
  return value
end

local function short(text, limit)
  text = text or ""
  if #text <= limit then return text end
  return string.sub(text, 1, limit - 1) .. "."
end

local function words(text, limit)
  local out, length = "", 0
  for word in string.gmatch(text or "", "%S+") do
    if length == 0 then
      out, length = word, #word
    elseif length + #word + 1 <= limit then
      out, length = out .. " " .. word, length + #word + 1
    else
      out, length = out .. "\n" .. word, #word
    end
  end
  return out
end

local function type_color(types)
  local text = string.lower(types or "")
  if string.find(text, "fire", 1, true) then return 0xe85d45 end
  if string.find(text, "water", 1, true) then return 0x4d9be6 end
  if string.find(text, "electric", 1, true) then return 0xf2c94c end
  if string.find(text, "grass", 1, true) then return 0x64b864 end
  if string.find(text, "earth", 1, true) then return 0xb58a5a end
  if string.find(text, "rock", 1, true) then return 0xa98f73 end
  return 0x7f8c9b
end

local function show_leds(color)
  badge.led.set_all(
    math.floor(color / 65536),
    math.floor((color % 65536) / 256),
    color % 256
  )
  badge.led.show()
end

local function revision_from(text)
  if not text then return nil end
  return tonumber(string.match(text, "revision=(%d+)"))
end

local function name_for(id)
  for _, pokemon in ipairs(roster) do
    if pokemon.id == id then return pokemon.name end
  end
  return "Unknown"
end

local function parse_world(text, revision)
  if revision_from(text) ~= revision then return nil end
  local parsed_roster, parsed_states, parsed_events = {}, {}, {}
  local event_by_id = {}

  for line in string.gmatch(text, "[^\r\n]+") do
    local id, name, caught, species, types, hp, attack, defense, speed, rarity = string.match(
      line,
      "^pokemon|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|[^|]*|[^|]*$"
    )
    if id and #parsed_roster < 24 then
      parsed_roster[#parsed_roster + 1] = {
        id = id, name = name, caught = caught, species = species, types = types,
        hp = tonumber(hp) or 0, attack = tonumber(attack) or 0,
        defense = tonumber(defense) or 0, speed = tonumber(speed) or 0,
        rarity = rarity,
      }
    else
      local state_id, x, y, mood, energy, activity = string.match(
        line, "^state|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$"
      )
      if state_id then
        parsed_states[state_id] = {
          x = clamp(tonumber(x) or 50, 0, 100),
          y = clamp(tonumber(y) or 50, 0, 100), mood = mood,
          energy = clamp(tonumber(energy) or 0, 0, 100), activity = activity,
        }
      else
        local event_id, event_revision, actor_id, target_id, kind, summary = string.match(
          line, "^event|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$"
        )
        if event_id and #parsed_events < 12 then
          local event = {
            id = event_id, revision = event_revision, actor_id = actor_id,
            target_id = target_id, kind = kind, summary = summary, dialogue = {},
          }
          parsed_events[#parsed_events + 1] = event
          event_by_id[event_id] = event
        else
          local dialogue_id, speaker_id, dialogue = string.match(
            line, "^dialogue|([^|]*)|([^|]*)|([^|]*)$"
          )
          local event = dialogue_id and event_by_id[dialogue_id]
          if event and #event.dialogue < 2 then
            event.dialogue[#event.dialogue + 1] = {speaker_id = speaker_id, text = dialogue}
          end
        end
      end
    end
  end
  return parsed_roster, parsed_states, parsed_events
end

local function page_start()
  return math.floor((pokemon_selected - 1) / 8) * 8 + 1
end

local function render_menu()
  for index = 1, #MODES do
    local selected = index == menu_selected
    menu_boxes[index]:set_color(selected and 0x31516e or 0x202a36)
    menu_boxes[index]:set_border(selected and 0xffffff or 0x46515f, selected and 2 or 1)
  end
  menu_status:set_text("SYNC REV " .. math.max(0, loaded_revision) .. "   " .. #roster .. " MONS")
  show_leds(0x456b9e)
end

local function render_pokedex()
  dex_count:set_text("POKEDEX  " .. #roster)
  dex_status:set_text("REV " .. math.max(0, loaded_revision))
  local start = page_start()
  for slot = 1, 8 do
    local pokemon = roster[start + slot - 1]
    if pokemon then
      grid_labels[slot]:set_text(short(pokemon.name, 10))
      grid_boxes[slot]:set_color((start + slot - 1 == pokemon_selected) and type_color(pokemon.types) or 0x242b36)
      grid_boxes[slot]:set_border((start + slot - 1 == pokemon_selected) and 0xffffff or 0x46515f, (start + slot - 1 == pokemon_selected) and 2 or 1)
    else
      grid_labels[slot]:set_text("")
      grid_boxes[slot]:set_color(0x1b2330)
      grid_boxes[slot]:set_border(0x384454, 1)
    end
  end
  local pokemon = roster[pokemon_selected]
  if not pokemon then
    dex_name:set_text("No Pokemon")
    dex_caught:set_text("Waiting for inbox")
    dex_species:set_text("")
    dex_type:set_text("")
    dex_stats_a:set_text("")
    dex_stats_b:set_text("")
    dex_rarity:set_text("")
    dex_avatar:set_color(0x46515f)
    return
  end
  dex_name:set_text(short(pokemon.name, 16))
  dex_caught:set_text("Caught: " .. short(pokemon.caught, 14))
  dex_species:set_text("Species:\n" .. words(pokemon.species, 17))
  dex_type:set_text("Type:\n" .. string.gsub(pokemon.types, "/", "/\n"))
  dex_stats_a:set_text("HP " .. pokemon.hp .. "  AT " .. pokemon.attack)
  dex_stats_b:set_text("DF " .. pokemon.defense .. "  SP " .. pokemon.speed)
  dex_rarity:set_text(string.upper(short(pokemon.rarity, 14)))
  dex_avatar:set_color(type_color(pokemon.types))
  show_leds(type_color(pokemon.types))
end

local function render_habitat()
  habitat_title:set_text("HABITAT  REV " .. math.max(0, loaded_revision))
  local selected = roster[habitat_selected]
  local selected_state = selected and states[selected.id] or nil
  if selected then
    habitat_detail:set_text(short(selected.name, 13) .. " - " .. short(selected_state and selected_state.activity or "waiting", 18))
    show_leds(type_color(selected.types))
  else
    habitat_detail:set_text("Waiting for Pokemon records")
    show_leds(0x6b5b95)
  end
  for index = 1, 6 do
    local pokemon = roster[index]
    if pokemon then
      local state = states[pokemon.id] or {x = 15 + index * 12, y = 40}
      habitat_actors[index]:set_pos(clamp(18 + math.floor(state.x * 2.5), 18, 272), clamp(44 + math.floor(state.y * 0.85), 44, 128))
      habitat_actors[index]:set_color(type_color(pokemon.types))
      habitat_actors[index]:set_border(index == habitat_selected and 0xffffff or 0x17202b, index == habitat_selected and 2 or 1)
      habitat_names[index]:set_text(short(pokemon.name, 5))
    else
      habitat_actors[index]:set_pos(-40, -40)
      habitat_names[index]:set_text("")
    end
  end
  local event = events[#events]
  if event then
    habitat_event:set_text(short(event.summary, 40))
    if #event.dialogue > 0 then
      local line = event.dialogue[#event.dialogue]
      habitat_talk:set_text(short(name_for(line.speaker_id), 10) .. ":\n" .. short(line.text, 34))
    else
      habitat_talk:set_text("")
    end
  else
    habitat_event:set_text("The habitat is quiet.")
    habitat_talk:set_text("")
  end
end

local function render_activity()
  activity_title:set_text("ACTIVITY  REV " .. math.max(0, loaded_revision))
  for slot = 1, 3 do
    local event = events[#events - slot + 1]
    if event then
      activity_lines[slot]:set_text(short(name_for(event.actor_id), 11) .. " -> " .. short(name_for(event.target_id), 11) .. "\n" .. words(short(event.summary, 58), 37))
    else
      activity_lines[slot]:set_text("")
    end
  end
  show_leds(0x4ca6a8)
end

local function render_current()
  if mode == "menu" then render_menu()
  elseif mode == "pokedex" then render_pokedex()
  elseif mode == "habitat" then render_habitat()
  else render_activity()
  end
end

local function add_background()
  local background = track(badge.ui.box(root, 320, 240))
  background:set_pos(0, 0)
  background:style({bg_color = 0x11161e})
end

local function open_menu()
  mode = "menu"
  clear_view()
  add_background()
  local title = track(badge.ui.label(root, "POKELIFE"))
  title:set_pos(28, 24)
  title:style({text_font = 20, text_color = 0xffffff})
  local subtitle = track(badge.ui.label(root, "A living Pokemon collection"))
  subtitle:set_pos(28, 48)
  subtitle:style({text_font = 14, text_color = 0xaebdce})
  for index = 1, #MODES do
    local box = track(badge.ui.box(root, 264, 34))
    box:set_pos(28, 76 + (index - 1) * 42)
    box:style({bg_color = 0x202a36, border_color = 0x46515f, border_width = 1, radius = 5})
    menu_boxes[index] = box
    local label = badge.ui.label(box, MODES[index])
    label:set_pos(12, 8)
    label:style({text_font = 16, text_color = 0xffffff})
  end
  menu_status = track(badge.ui.label(root, "SYNC REV 0   0 MONS"))
  menu_status:set_pos(28, 216)
  menu_status:style({text_font = 14, text_color = 0xaebdce})
  render_menu()
end

local function open_pokedex()
  mode = "pokedex"
  clear_view()
  add_background()
  local left = track(badge.ui.box(root, 156, 228))
  left:set_pos(2, 6)
  left:style({bg_color = 0x171e29, border_color = 0x384454, border_width = 1, radius = 5})
  local right = track(badge.ui.box(root, 154, 228))
  right:set_pos(162, 6)
  right:style({bg_color = 0x1b2330, border_color = 0x526378, border_width = 1, radius = 5})
  dex_count = track(badge.ui.label(root, "POKEDEX  0"))
  dex_count:set_pos(8, 14); dex_count:style({text_font = 16, text_color = 0xffffff})
  dex_status = track(badge.ui.label(root, "REV 0"))
  dex_status:set_pos(8, 216); dex_status:style({text_font = 14, text_color = 0xaebdce})
  for slot = 1, 8 do
    local column, row = (slot - 1) % 2, math.floor((slot - 1) / 2)
    local cell = track(badge.ui.box(root, 72, 38))
    cell:set_pos(5 + column * 76, 40 + row * 43)
    cell:style({bg_color = 0x242b36, border_color = 0x46515f, border_width = 1, radius = 4})
    grid_boxes[slot] = cell
    local label = badge.ui.label(cell, "")
    label:set_pos(4, 4); label:style({text_font = 14, text_color = 0xffffff})
    grid_labels[slot] = label
  end
  local selected = track(badge.ui.label(root, "SELECTED"))
  selected:set_pos(170, 15); selected:style({text_font = 14, text_color = 0xaebdce})
  dex_avatar = track(badge.ui.box(root, 32, 32))
  dex_avatar:set_pos(268, 30); dex_avatar:style({bg_color = 0x46515f, border_color = 0xffffff, border_width = 1, radius = 5})
  dex_name = track(badge.ui.label(root, "")); dex_name:set_pos(170, 43); dex_name:style({text_font = 18, text_color = 0xffffff})
  dex_caught = track(badge.ui.label(root, "")); dex_caught:set_pos(170, 70); dex_caught:style({text_font = 14, text_color = 0xaebdce})
  dex_species = track(badge.ui.label(root, "")); dex_species:set_pos(170, 94); dex_species:style({text_font = 14, text_color = 0xffffff})
  dex_type = track(badge.ui.label(root, "")); dex_type:set_pos(170, 142); dex_type:style({text_font = 14, text_color = 0xaebdce})
  dex_stats_a = track(badge.ui.label(root, "")); dex_stats_a:set_pos(170, 174); dex_stats_a:style({text_font = 14, text_color = 0xffffff})
  dex_stats_b = track(badge.ui.label(root, "")); dex_stats_b:set_pos(170, 192); dex_stats_b:style({text_font = 14, text_color = 0xffffff})
  dex_rarity = track(badge.ui.label(root, "")); dex_rarity:set_pos(170, 214); dex_rarity:style({text_font = 14, text_color = 0xf2c94c})
  render_pokedex()
end

local function open_habitat()
  mode = "habitat"
  clear_view()
  add_background()
  local panel = track(badge.ui.box(root, 308, 228))
  panel:set_pos(6, 6); panel:style({bg_color = 0x17212a, border_color = 0x526378, border_width = 1, radius = 5})
  habitat_title = track(badge.ui.label(root, "HABITAT  REV 0"))
  habitat_title:set_pos(16, 15); habitat_title:style({text_font = 16, text_color = 0xffffff})
  local field = track(badge.ui.box(root, 288, 122))
  field:set_pos(16, 35); field:style({bg_color = 0x243542, border_color = 0x597182, border_width = 1, radius = 4})
  for index = 1, 6 do
    local actor = track(badge.ui.box(root, 30, 20))
    actor:set_pos(-40, -40); actor:style({bg_color = 0x46515f, border_color = 0x17202b, border_width = 1, radius = 4})
    habitat_actors[index] = actor
    local label = badge.ui.label(actor, "")
    label:set_pos(2, 3); label:style({text_font = 14, text_color = 0xffffff})
    habitat_names[index] = label
  end
  habitat_detail = track(badge.ui.label(root, "")); habitat_detail:set_pos(16, 164); habitat_detail:style({text_font = 14, text_color = 0xffffff})
  habitat_event = track(badge.ui.label(root, "")); habitat_event:set_pos(16, 184); habitat_event:style({text_font = 14, text_color = 0xaebdce})
  habitat_talk = track(badge.ui.label(root, "")); habitat_talk:set_pos(16, 204); habitat_talk:style({text_font = 14, text_color = 0xf2c94c})
  render_habitat()
end

local function open_activity()
  mode = "activity"
  clear_view()
  add_background()
  local panel = track(badge.ui.box(root, 308, 228))
  panel:set_pos(6, 6); panel:style({bg_color = 0x1b2330, border_color = 0x526378, border_width = 1, radius = 5})
  activity_title = track(badge.ui.label(root, "ACTIVITY  REV 0"))
  activity_title:set_pos(16, 15); activity_title:style({text_font = 16, text_color = 0xffffff})
  for slot = 1, 3 do
    local line = track(badge.ui.label(root, ""))
    line:set_pos(18, 48 + (slot - 1) * 58); line:style({text_font = 14, text_color = slot == 1 and 0xffffff or 0xaebdce})
    activity_lines[slot] = line
  end
  render_activity()
end

local function load_inbox()
  local before = revision_from(badge.fs.read("inbox.ready"))
  if not before or before <= loaded_revision then return end
  local text = badge.fs.read("inbox.tmp")
  local after = revision_from(badge.fs.read("inbox.ready"))
  if before ~= after then return end
  local parsed_roster, parsed_states, parsed_events = parse_world(text, before)
  if not parsed_roster then return end
  roster, states, events = parsed_roster, parsed_states, parsed_events
  loaded_revision = before
  pokemon_selected = clamp(pokemon_selected, 1, math.max(1, #roster))
  habitat_selected = clamp(habitat_selected, 1, math.max(1, #roster))
  render_current()
end

function on_enter(new_root)
  root = new_root
  open_menu()
  badge.sys.log("PokeLife menu ready")
end

function on_tick()
  local now = badge.sys.ms()
  if now >= next_inbox_check then
    next_inbox_check = now + 1000
    load_inbox()
  end
end

function on_button(button, kind)
  if kind ~= badge.input.KIND.PRESSED then return end
  local B = badge.input.BUTTON
  if mode == "menu" then
    if button == B.UP then
      menu_selected = (menu_selected - 2) % #MODES + 1
      render_menu()
    elseif button == B.DOWN then
      menu_selected = menu_selected % #MODES + 1
      render_menu()
    elseif button == B.A or button == B.RIGHT then
      if menu_selected == 1 then open_pokedex()
      elseif menu_selected == 2 then open_habitat()
      else open_activity() end
    end
    return
  end
  if button == B.B then open_menu(); return end
  if mode == "pokedex" and #roster > 0 then
    local target = pokemon_selected
    if button == B.LEFT and (target - 1) % 2 == 1 then target = target - 1
    elseif button == B.RIGHT and (target - 1) % 2 == 0 and target < #roster then target = target + 1
    elseif button == B.UP and target > 2 then target = target - 2
    elseif button == B.DOWN and target + 2 <= #roster then target = target + 2 end
    if target ~= pokemon_selected then pokemon_selected = target; render_pokedex() end
  elseif mode == "habitat" and #roster > 0 then
    if button == B.LEFT or button == B.UP then habitat_selected = (habitat_selected - 2) % #roster + 1
    elseif button == B.RIGHT or button == B.DOWN then habitat_selected = habitat_selected % #roster + 1 end
    render_habitat()
  end
end

function on_exit()
  badge.led.clear()
  badge.led.show()
end
