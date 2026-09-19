--[==[badge-app
slug=habitat
name=Shutterdex Habitat
icon=HAB
api=2
heap_kb=96
wake_lock=1
]==]

-- Shutterdex Habitat
-- Reads the same atomic snapshot as the other badge apps.  It deliberately
-- uses coloured monograms instead of decoded image assets so it remains a
-- small, dependable live view on the badge.

local MAX_VISIBLE = 4
local POLL_MS = 800
local FRAME_MS = 80
-- A server tick can only change a position by a few world units.  Keep the
-- arrival animation long enough, and add a small walking bob, so that a new
-- event is unmistakably visible on the physical badge screen.
local MIN_TRAVEL_MS = 2600

local C = {
  bg = 0x0d1912, panel = 0x193425, border = 0x608a70,
  text = 0xf0f7ef, muted = 0xa9c3ae, accent = 0x55d494,
  grass = 0x315e42, water = 0x2e7195, leaf = 0x53783f,
}

local TYPE = {
  electric = 0xf2c94c, earth = 0xb6895b, grass = 0x6cc96b,
  fire = 0xee725d, water = 0x5daeea, air = 0xb6d8ed,
  dark = 0x85779d,
}

local world = {
  revision = -1, pokemon = {}, states = {}, motions = {},
  latest = nil, selected = 1, positions = {}, next_poll = 0, next_frame = 0,
  travel_notice = "WAITING FOR A WORLD", travel_active = false, shown_notice = nil,
}

local ui = {actors = {}}

local function read(path)
  return badge.fs.read(path) or ""
end

local function revision(text)
  return tonumber(string.match(text or "", "revision=(%d+)"))
end

local function fields(line)
  local out = {}
  for value in string.gmatch((line or "") .. "|", "([^|]*)|") do
    out[#out + 1] = value
  end
  return out
end

local function number(value, fallback)
  return tonumber(value) or fallback
end

local function clamp(value, low, high)
  if value < low then return low end
  if value > high then return high end
  return value
end

local function shorten(text, size)
  text = text or ""
  if #text <= size then return text end
  return string.sub(text, 1, size - 3) .. "..."
end

local function wrap(text, width)
  text = shorten(text, width * 2)
  if #text <= width then return text end
  local cut = width
  while cut > 1 and string.sub(text, cut, cut) ~= " " do cut = cut - 1 end
  if cut == 1 then cut = width end
  return string.sub(text, 1, cut) .. "\n" .. shorten(string.sub(text, cut + 1), width)
end

local function pretty(text)
  return string.gsub(text or "", "_", " ")
end

local function color_for(kind)
  local key = string.match(string.lower(kind or ""), "^[^/ ]+") or ""
  return TYPE[key] or C.accent
end

local function point(state, motion)
  local x = motion and motion.x or state.x
  local y = motion and motion.y or state.y
  return clamp(16 + math.floor(x * 2.36), 16, 278),
         clamp(47 + math.floor(y * 0.78), 47, 143)
end

local function parse_snapshot(text, expected)
  if revision(text) ~= expected then return nil end
  local next = {pokemon = {}, states = {}, motions = {}, latest = nil}

  for line in string.gmatch(text, "[^\r\n]+") do
    local f = fields(line)
    if f[1] == "pokemon" and #next.pokemon < MAX_VISIBLE and f[2] ~= "" then
      next.pokemon[#next.pokemon + 1] = {id = f[2], name = f[3], element = f[6]}
    elseif f[1] == "state" and f[2] ~= "" then
      next.states[f[2]] = {x = number(f[3], 50), y = number(f[4], 50), activity = f[7]}
    elseif f[1] == "motion" and f[2] ~= "" then
      next.motions[f[2]] = {
        x = number(f[3], 50), y = number(f[4], 50),
        duration = clamp(number(f[5], 1200), 250, 5000),
      }
    elseif f[1] == "event" and f[2] ~= "" then
      next.latest = {id = f[2], actor = f[4], target = f[5], kind = f[6], text = f[7]}
    elseif f[1] == "dialogue" and next.latest and f[2] == next.latest.id then
      next.latest.speaker = f[3]
      next.latest.text = f[4]
    end
  end
  return next
end

local function install(next, rev)
  local now = badge.sys.ms()
  local old_states = world.states
  world.pokemon, world.states = next.pokemon, next.states
  world.motions, world.latest, world.revision = next.motions, next.latest, rev
  world.travel_notice = "WORLD UPDATED"
  world.travel_active = false
  if world.selected > #world.pokemon then world.selected = 1 end

  for i = 1, #world.pokemon do
    local mon = world.pokemon[i]
    local state = world.states[mon.id] or {x = 50, y = 50}
    local previous_state = old_states[mon.id]
    local motion = world.motions[mon.id]
    local target_x, target_y = point(state, motion)
    local old = world.positions[mon.id]
    if old then
      old.start_x, old.start_y = old.x, old.y
      old.target_x, old.target_y = target_x, target_y
      old.started = now
      old.duration = math.max(motion and motion.duration or 1200, MIN_TRAVEL_MS)
      old.from_world_x = previous_state and previous_state.x or state.x
      old.from_world_y = previous_state and previous_state.y or state.y
      old.to_world_x, old.to_world_y = state.x, state.y
      if not world.travel_active and (old.start_x ~= target_x or old.start_y ~= target_y) then
        world.travel_active = true
        world.travel_notice = "MOVING " .. shorten(mon.name, 10) .. "  " ..
          old.from_world_x .. "," .. old.from_world_y .. " > " ..
          old.to_world_x .. "," .. old.to_world_y
      end
    else
      world.positions[mon.id] = {
        x = target_x, y = target_y, start_x = target_x, start_y = target_y,
        target_x = target_x, target_y = target_y, started = now,
        duration = math.max(motion and motion.duration or 1200, MIN_TRAVEL_MS),
        from_world_x = state.x, from_world_y = state.y,
        to_world_x = state.x, to_world_y = state.y,
      }
    end
  end
end

local function load()
  local before = revision(read("inbox.ready"))
  if not before then return false end
  local text = read("inbox.tmp")
  local after = revision(read("inbox.ready"))
  if before ~= after or revision(text) ~= before or before == world.revision then return false end
  local next = parse_snapshot(text, before)
  if not next then return false end
  install(next, before)
  return true
end

local function label(parent, x, y, w, h, text, style)
  local item = badge.ui.label(parent, text or "")
  item:set_pos(x, y)
  item:set_size(w, h)
  item:style(style)
  return item
end

local function box(parent, x, y, w, h, style)
  local item = badge.ui.box(parent, w, h)
  item:set_pos(x, y)
  item:style(style)
  return item
end

local function actor(root)
  local trail = box(root, -40, -40, 8, 4, {
    bg_color = C.accent, border_width = 0, radius = 2,
  })
  local dot = box(root, -40, -40, 28, 28, {
    bg_color = C.accent, border_color = C.text, border_width = 1, radius = 14,
  })
  local initials = label(dot, 2, 6, 24, 16, "", {
    text_font = 14, text_color = C.bg, text_align = "center",
  })
  local tag = box(root, -40, -40, 56, 14, {
    bg_color = C.panel, border_color = C.border, border_width = 1, radius = 3,
  })
  local name = label(tag, 2, 0, 52, 14, "", {
    text_font = 14, text_color = C.text, text_align = "center",
  })
  return {trail = trail, dot = dot, initials = initials, tag = tag, name = name}
end

local function build(root)
  box(root, 0, 0, 320, 240, {bg_color = C.bg, border_width = 0})
  ui.title = label(root, 10, 9, 180, 22, "HABITAT", {text_font = 20, text_color = C.text})
  ui.status = label(root, 180, 12, 130, 14, "SYNC --", {
    text_font = 14, text_color = C.muted, text_align = "right",
  })
  box(root, 8, 35, 304, 137, {
    bg_color = C.grass, border_color = C.border, border_width = 1, radius = 7,
  })
  box(root, 210, 52, 77, 23, {bg_color = C.water, border_width = 0, radius = 12})
  box(root, 24, 58, 62, 18, {bg_color = C.leaf, border_width = 0, radius = 9})
  box(root, 237, 145, 51, 17, {bg_color = C.leaf, border_width = 0, radius = 9})
  ui.motion = label(root, 17, 39, 180, 14, "WAITING FOR A WORLD", {
    text_font = 14, text_color = C.text,
  })
  for i = 1, MAX_VISIBLE do ui.actors[i] = actor(root) end
  ui.bubble = box(root, 8, 178, 304, 45, {
    bg_color = C.panel, border_color = C.accent, border_width = 1, radius = 7,
  })
  ui.speaker = label(ui.bubble, 9, 4, 286, 14, "WAITING FOR A MOMENT", {
    text_font = 14, text_color = C.accent,
  })
  ui.message = label(ui.bubble, 9, 19, 286, 25, "Connect the bridge to publish a world.", {
    text_font = 14, text_color = C.text,
  })
  ui.footer = label(root, 10, 226, 300, 13, "LEFT/RIGHT select", {
    text_font = 14, text_color = C.muted, text_align = "center",
  })
end

local function hide(actor)
  actor.trail:hidden(true)
  actor.dot:hidden(true)
  actor.tag:hidden(true)
end

local function name_for(id)
  for i = 1, #world.pokemon do
    if world.pokemon[i].id == id then return world.pokemon[i].name end
  end
  return "Someone"
end

local function draw_actor(index, mon, position, walking)
  local item = ui.actors[index]
  local x, y = math.floor(position.x), math.floor(position.y)
  local bob = walking and ((math.floor((badge.sys.ms() - position.started) / 150) % 2 == 0) and -4 or 2) or 0
  local trail_width = math.floor(clamp(math.abs(x - position.start_x) + 8, 8, 42))
  item.trail:set_pos(clamp(math.min(x, position.start_x) + 10, 10, 300 - trail_width), y + 12)
  item.trail:set_size(trail_width, 4)
  item.trail:set_color(color_for(mon.element))
  item.trail:hidden(not walking)
  item.dot:set_pos(x, y + bob)
  item.dot:set_color(color_for(mon.element))
  item.initials:set_text(string.upper(string.sub(mon.name or "?", 1, 2)))
  item.tag:set_pos(clamp(x - 13, 10, 254), y + bob + 29)
  item.name:set_text(shorten(mon.name, 8))
  local selected = index == world.selected
  item.tag:set_color(selected and C.accent or C.panel)
  item.tag:set_border(selected and C.text or C.border, selected and 2 or 1)
  item.dot:hidden(false)
  item.tag:hidden(false)
  item.tag:bring_to_front()
end

local function draw_message()
  local event = world.latest
  if not event then
    ui.speaker:set_text("WAITING FOR A MOMENT")
    ui.message:set_text("The next server event will appear here.")
    return
  end
  local speaker = event.speaker or event.actor
  ui.speaker:set_text(name_for(speaker) .. "  ·  " .. string.upper(pretty(event.kind)))
  ui.message:set_text(wrap(event.text, 40))
end

local function draw_motion_notice()
  local text = world.travel_notice
  if not world.travel_active then
    local mon = world.pokemon[world.selected]
    local pos = mon and world.positions[mon.id]
    if pos then text = "AT " .. shorten(mon.name, 10) .. "  " ..
      pos.to_world_x .. "," .. pos.to_world_y end
  end
  if text ~= world.shown_notice then
    world.shown_notice = text
    ui.motion:set_text(text)
  end
end

local function draw()
  ui.status:set_text("SYNC " .. math.max(world.revision, 0))
  for i = 1, MAX_VISIBLE do
    local mon = world.pokemon[i]
    if mon then draw_actor(i, mon, world.positions[mon.id]) else hide(ui.actors[i]) end
  end
  draw_motion_notice()
  draw_message()
end

local function animate(now)
  local any_moving = false
  for i = 1, #world.pokemon do
    local mon = world.pokemon[i]
    local pos = world.positions[mon.id]
    local progress = clamp((now - pos.started) / pos.duration, 0, 1)
    pos.x = pos.start_x + (pos.target_x - pos.start_x) * progress
    pos.y = pos.start_y + (pos.target_y - pos.start_y) * progress
    local walking = progress < 1 and (pos.start_x ~= pos.target_x or pos.start_y ~= pos.target_y)
    any_moving = any_moving or walking
    draw_actor(i, mon, pos, walking)
  end
  if world.travel_active and not any_moving then
    world.travel_active = false
    draw_motion_notice()
  end
end

function on_enter(root)
  build(root)
  load()
  draw()
end

function on_tick()
  local now = badge.sys.ms()
  if now >= world.next_poll then
    world.next_poll = now + POLL_MS
    if load() then draw() end
  end
  if now >= world.next_frame then
    world.next_frame = now + FRAME_MS
    animate(now)
  end
end

function on_button(button, kind)
  if kind ~= badge.input.KIND.PRESSED or #world.pokemon == 0 then return end
  if button == badge.input.BUTTON.LEFT or button == badge.input.BUTTON.UP then
    world.selected = (world.selected - 2) % #world.pokemon + 1
  elseif button == badge.input.BUTTON.RIGHT or button == badge.input.BUTTON.DOWN then
    world.selected = world.selected % #world.pokemon + 1
  else
    return
  end
  draw()
end

function on_exit() end
