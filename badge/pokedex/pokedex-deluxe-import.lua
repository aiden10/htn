--[==[badge-app
slug=pokedex
name=PokeLife
icon=DEX
api=2
heap_kb=96
wake_lock=1
]==]
-- PokeLife deluxe: a compact, file-backed Pokédex and habitat.
local r,m,mode,sel,rev,due=nil,{},0,1,-1,0
local p,st,ev,card,bl,txt,pic,field={},{},{},{},{},{},{},nil
local menu={"POKEDEX","HABITAT","ACTIVITY"}
local function lim(v,a,b) if v<a then return a elseif v>b then return b end return v end
local function cut(v,n) v=v or "" if #v>n then return string.sub(v,1,n-1).."…" end return v end
local function shown(v) return string.gsub(v or "", "_", " ") end
local function name(q) return (q and q[2]) or "EMPTY" end
local function rv(v) return tonumber(string.match(v or "","revision=(%d+)")) end
local function label(i,x,y,w,h,s,c)
  local q=txt[i] q:set_pos(x,y) q:set_size(w,h) q:set_text(s or "")
  if c then q:set_color(c) end
end
local function panel(i,x,y,w,h,c,s)
  local q=card[i] q:set_pos(x,y) q:set_size(w,h) q:set_color(c or 0x284832) bl[i]:set_text(s or "")
end
local function hidepics() for i=1,8 do if pic[i] then pic[i]:hidden(true) end end end
local function sprite(i,key,x,y)
  local path=(key or "")..".bin"
  -- The bridge installs sprite files before it commits inbox.ready.  Avoid an
  -- existence probe here: this firmware can report stale existence metadata
  -- immediately after a binary serial transfer.
  if not key or key=="" then return end
  if not pic[i] then pic[i]=badge.ui.image(r,path) else pic[i]:set_src(path) end
  pic[i]:set_pos(x,y) pic[i]:hidden(false)
end
local function wipe()
  if field then field:hidden(true) end
  hidepics()
  for i=1,8 do panel(i,-340,-40,1,1,0," ") end
  for i=1,16 do label(i,-340,-40,1,1," ") end
end
local function readworld()
  -- Read exactly like the proven first viewer: the app sandbox accepts these
  -- reads directly, while an existence check can be stale during a bridge copy.
  local ready=badge.fs.read("inbox.ready")
  local v=rv(ready)
  if not v or v==rev then return end
  local raw=badge.fs.read("inbox.tmp") or ""
  if rv(raw)~=v or rv(badge.fs.read("inbox.ready"))~=v then return end
  local np,ns,ne={},{},{}
  for line in raw:gmatch("[^\r\n]+") do
    local a,b,c,d,e,f,g,h,i,j,k,l=string.match(line,"^pokemon|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$")
    if a then np[#np+1]={a,b,c,d,e,f,g,h,i,j,k,l} end
    local x,y,z,aa,ab,ac=string.match(line,"^state|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)$")
    if x then ns[x]={tonumber(y) or 50,tonumber(z) or 50,aa or "calm",tonumber(ab) or 60,ac or "wandering"} end
    local t,u,w,x=string.match(line,"^event|[^|]*|[^|]*|([^|]*)|([^|]*)|([^|]*)|([^|]*)$") if t then ne[#ne+1]={t,w,x} end
  end
  p,st,ev,rev=np,ns,ne,v
end
local function menuview()
  label(1,16,13,288,25,"POKELIFE",0x9fe7bc); label(2,16,38,288,16,"Your living collection",0xb9cdbb)
  for i=1,3 do
    local on=(i==sel); local y=63+(i-1)*55
    panel(i,14,y,292,45,on and 0x28785c or 0x25412c,"")
    label(2+i,31,y+8,190,19,menu[i],on and 0xffffff or 0xd2e6d3)
    label(7+i,31,y+27,240,13,i==1 and "Browse captured creatures" or (i==2 and "Watch them roam together" or "Read recent moments"),0xb5c6b5)
  end
  label(12,16,224,288,13,"A select   B back",0x93ab95)
end
local function dexview()
  local n=#p local page=math.floor((sel-1)/4)*4
  label(1,8,8,304,19,"POKEDEX",0x9fe7bc); label(2,8,27,145,12,n.." CREATURES",0xb5c6b5)
  panel(5,160,38,152,181,0x1b3928,"")
  for i=1,4 do
    local ix=page+i; local q=p[ix]; local col=((ix==sel) and 0x2c8067 or 0x244832)
    local x=8+((i-1)%2)*74; local y=42+math.floor((i-1)/2)*86
    panel(i,x,y,68,78,col,"")
    if q then sprite(i,q[12],x+14,y+6); label(2+i,x+5,y+53,59,15,cut(name(q),10),0xffffff); label(6+i,x+5,y+68,59,10,cut(shown(q[5]),10),0xc4ddca) end
  end
  local q=p[sel]
  if q then
    sprite(5,q[12],169,46); label(11,216,48,90,17,cut(name(q),12),0xffffff); label(12,170,91,132,24,cut(q[4],25),0xc2d7c7)
    label(13,170,119,132,14,"TYPE  "..string.upper(cut(shown(q[5]),15)),0x8ee7b9)
    label(14,170,137,132,14,"RARITY  "..string.upper(cut(q[10],13)),0xe1ce8d)
    label(15,170,160,132,14,"HP "..q[6].."  ATK "..q[7].."  DEF "..q[8],0xbcd1c0)
    label(16,170,177,132,29,cut(q[11],48),0xd5e0d6)
  else label(11,174,73,130,36,"No Pokemon\nyet",0xc2d7c7) end
  label(1,8,224,145,13,"D-pad browse",0x93ab95)
end
local function habitat()
  label(1,8,8,304,19,"HABITAT",0x9fe7bc); label(2,8,25,304,13,"LIVE TERRARIUM",0xb5c6b5)
  field:set_pos(8,40) field:set_size(304,150) field:set_color(0x315c43) field:hidden(false)
  panel(5,22,58,75,23,0x416d3d,""); panel(6,229,148,64,23,0x486f44,""); panel(7,152,71,52,31,0x397899,"")
  for i=1,math.min(#p,4) do
    local q=p[i]; local v=st[q[1]] or {50,50,"calm",60,"wandering"}
    local x=lim(15+math.floor(v[1]*2.35),15,260); local y=lim(43+math.floor(v[2]*1.16),43,136)
    sprite(i,q[12],x,y); panel(i,x,y+39,48,14,(i==sel) and 0x2c8067 or 0x254b36,cut(name(q),8))
  end
  local q=p[sel] or p[1]
  if q then local v=st[q[1]] or {0,0,"calm",60,"wandering"}
    panel(8,8,199,304,32,0x1c3929,""); label(11,17,204,115,13,name(q),0xffffff); label(12,17,218,280,11,cut(shown(v[5]).." • "..shown(v[3]).." • energy "..v[4],42),0xb9d6c0)
  else label(11,16,208,280,16,"Waiting for the first creature…",0xc2d7c7) end
end
local function activity()
  label(1,8,8,304,19,"ACTIVITY",0x9fe7bc); label(2,8,27,304,13,"RECENT MOMENTS",0xb5c6b5)
  if #ev==0 then label(3,16,72,280,28,"No moments yet.\nThe simulation will write them here.",0xc2d7c7) end
  for i=1,math.min(#ev,3) do local q=ev[#ev-i+1]; local y=48+(i-1)*57
    panel(i,10,y,300,48,0x244832,""); label(3+i,21,y+8,277,14,cut(q[3],45),0xe6f2e7); label(7+i,21,y+28,277,12,cut((q[1] or "").."  "..(q[2] or ""),43),0xb9d6c0)
  end
end
local function draw()
  wipe() if mode==0 then menuview() elseif mode==1 then dexview() elseif mode==2 then habitat() else activity() end
end
function on_enter(root)
  r=root
  local bg=badge.ui.box(r,320,240); bg:set_pos(0,0); bg:style({bg_color=0x13231a,border_width=0})
  field=badge.ui.box(r,1,1); field:style({bg_color=0x315c43,border_color=0x86b994,border_width=1,radius=4})
  for i=1,8 do
    card[i]=badge.ui.box(r,1,1); card[i]:style({bg_color=0x284832,border_color=0x8bb99a,border_width=1,radius=4})
    bl[i]=badge.ui.label(card[i],""); bl[i]:set_pos(3,2); bl[i]:style({text_font=14,text_color=0xffffff})
  end
  for i=1,16 do txt[i]=badge.ui.label(r,""); txt[i]:style({text_font=14,text_color=0xd2e0ec}) end
  readworld(); draw()
end
function on_tick()
  local now=badge.sys.ms() if now>=due then due=now+900; local old=rev; readworld(); if old~=rev then draw() end end
end
function on_button(b,e)
  if e~=badge.input.KIND.PRESSED then return end
  local k=badge.input.BUTTON
  if mode==0 then
    if b==k.UP then sel=(sel-2)%3+1
    elseif b==k.DOWN then sel=sel%3+1
    elseif b==k.A or b==k.RIGHT then mode=sel; sel=1 end
  else
    if b==k.B then mode=0; sel=1
    elseif #p>0 then
      if mode==1 then
        if b==k.LEFT and (sel-1)%2==1 then sel=sel-1
        elseif b==k.RIGHT and (sel-1)%2==0 and sel<#p then sel=sel+1
        elseif b==k.UP and sel>2 then sel=sel-2
        elseif b==k.DOWN and sel+2<=#p then sel=sel+2 end
      elseif mode==2 and (b==k.LEFT or b==k.UP or b==k.RIGHT or b==k.DOWN) then
        if b==k.LEFT or b==k.UP then sel=(sel-2)%#p+1 else sel=sel%#p+1 end
      end
    end
  end
  draw()
end
function on_exit() end
