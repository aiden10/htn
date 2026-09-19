--[==[badge-app
slug=pokedex
name=PokeLife
icon=DEX
api=2
heap_kb=96
wake_lock=1
]==]

-- Compact PokeLife viewer. The PC bridge writes inbox.tmp, then inbox.ready.
local r,h,m=nil,{},"menu"
local p,s,e,rev,due,sel={},{},{},-1,0,1
local b,bl,tx={},{},{}
local M={"POKEDEX","HABITAT","ACTIVITY"}
local function lim(n,a,z) if n<a then return a end if n>z then return z end return n end
local function cut(x,n) x=x or "" if #x>n then return string.sub(x,1,n-1).."." end return x end
local function rv(x) if not x then return nil end return tonumber(string.match(x,"revision=(%d+)")) end
local function name(t,id) for i=1,#t do if t[i][1]==id then return t[i][2] end end return "?" end
local function box(i,x,y,w,z,v,c)
  b[i]:set_pos(x,y); b[i]:set_size(w,z); b[i]:set_color(c or 0x31495a); bl[i]:set_text(v or "")
end
local function text(i,x,y,v,c)
  tx[i]:set_pos(x,y); tx[i]:set_text(v or ""); if c then tx[i]:set_color(c) end
end
local function wipe()
  for i=1,8 do box(i,-50,-50,1,1,"") end
  for i=1,5 do text(i,-50,-50,"") end
end
local function redraw()
  wipe()
  if m=="menu" then
    text(1,16,15,"POKELIFE",0xffffff); text(2,16,38,"Choose a view",0xaac0d0)
    for i=1,3 do box(i,18,68+(i-1)*46,284,34,(i==sel and "> " or "  ")..M[i],i==sel and 0x397c99 or 0x283844) end
    return
  end
  if m=="dex" then
    text(1,12,9,"POKEDEX  R"..rev,0xffffff); text(2,12,220,"B: menu",0xaac0d0)
    if #p==0 then text(3,12,70,"Waiting for inbox data..."); return end
    local page=math.floor((sel-1)/8)*8
    for i=1,8 do
      local q=p[page+i]
      if q then
        local x=10+((i-1)%2)*74; local y=35+math.floor((i-1)/2)*42
        box(i,x,y,68,34,cut(q[2],9),page+i==sel and 0x428fb2 or 0x31495a)
      end
    end
    local q=p[sel]
    text(3,165,37,cut(q[2],18),0xffffff); text(4,165,65,cut(q[4],20),0xd2e0ec)
    text(5,165,96,cut(q[5],20),0xf2c94c); text(1,165,131,"Caught "..cut(q[3],15),0xaac0d0)
    return
  end
  if m=="hab" then
    text(1,12,9,"HABITAT  R"..rev,0xffffff); text(2,12,220,"D-pad: select   B: menu",0xaac0d0)
    if #p==0 then text(3,12,70,"Waiting for Pokemon..."); return end
    for i=1,math.min(#p,6) do
      local q,v=p[i],s[p[i][1]] or {50,50,"idle",0,"exploring"}
      local x,y=12+math.floor(v[1]*2.55),30+math.floor(v[2]*1.55)
      box(i,lim(x,12,276),lim(y,30,178),31,20,cut(q[2],4),i==sel and 0x5ea4c6 or 0x476678)
    end
    local q,v=p[sel],s[p[sel][1]] or {0,0,"idle",0,"exploring"}
    text(3,12,188,q[2]..": "..cut(v[5],23),0xffffff); text(4,12,205,"Mood: "..cut(v[3],16),0xf2c94c)
    return
  end
  text(1,12,9,"ACTIVITY  R"..rev,0xffffff); text(2,12,220,"B: menu",0xaac0d0)
  if #e==0 then text(3,12,65,"No activity yet."); return end
  for i=1,math.min(#e,3) do box(i,12,37+(i-1)*57,296,45,cut(e[i],39),i==1 and 0x31495a or 0x283844) end
end
local function read_world()
  local a=rv(badge.fs.read("inbox.ready")); if not a or a<=rev then return end
  local x=badge.fs.read("inbox.tmp")
  if a~=rv(badge.fs.read("inbox.ready")) or rv(x)~=a then return end
  local np,ns,ne={},{},{}
  for line in string.gmatch(x,"[^\r\n]+") do
    local id,n,d,k,ty=string.match(line,"^pokemon|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)")
    if id and #np<24 then np[#np+1]={id,n,d,k,ty}
    else
      local z,px,py,mood,en,act=string.match(line,"^state|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)")
      if z then ns[z]={tonumber(px) or 50,tonumber(py) or 50,mood,tonumber(en) or 0,act}
      else
        local actor,target,kind,summary=string.match(line,"^event|[^|]*|[^|]*|([^|]*)|([^|]*)|([^|]*)|(.+)$")
        if actor then ne[#ne+1]=cut(name(np,actor).." "..kind.." "..name(np,target)..": "..summary,55) end
      end
    end
  end
  p,s,e,rev=np,ns,ne,a; sel=lim(sel,1,math.max(1,#p)); redraw()
end
function on_enter(root)
  r=root
  local bg=badge.ui.box(r,320,240); bg:set_pos(0,0); bg:style({bg_color=0x14202a,border_width=0})
  for i=1,8 do
    b[i]=badge.ui.box(r,1,1); b[i]:style({bg_color=0x31495a,border_color=0x7b9bad,border_width=1,radius=3})
    bl[i]=badge.ui.label(b[i],""); bl[i]:set_pos(3,8); bl[i]:style({text_font=14,text_color=0xffffff})
  end
  for i=1,5 do tx[i]=badge.ui.label(r,""); tx[i]:style({text_font=14,text_color=0xd2e0ec}) end
  read_world(); redraw()
end
function on_tick()
  local now=badge.sys.ms(); if now>=due then due=now+1000; read_world() end
end
function on_button(key,kind)
  if kind~=badge.input.KIND.PRESSED then return end
  local k=badge.input.BUTTON
  if m=="menu" then
    if key==k.UP then sel=(sel-2)%3+1
    elseif key==k.DOWN then sel=sel%3+1
    elseif key==k.A or key==k.RIGHT then m=sel==1 and "dex" or (sel==2 and "hab" or "act"); sel=1 end
  elseif key==k.B then m="menu"; sel=1
  elseif #p>0 then
    if m=="dex" then
      if key==k.LEFT and (sel-1)%2==1 then sel=sel-1
      elseif key==k.RIGHT and (sel-1)%2==0 and sel<#p then sel=sel+1
      elseif key==k.UP and sel>2 then sel=sel-2
      elseif key==k.DOWN and sel+2<=#p then sel=sel+2 end
    elseif m=="hab" and (key==k.LEFT or key==k.UP or key==k.RIGHT or key==k.DOWN) then
      if key==k.LEFT or key==k.UP then sel=(sel-2)%#p+1 else sel=sel%#p+1 end
    end
  end
  redraw()
end
