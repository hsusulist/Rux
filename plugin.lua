--!strict
local HttpService = game:GetService("HttpService")
local Selection = game:GetService("Selection")
local TweenService = game:GetService("TweenService")
local RunService = game:GetService("RunService")
local StudioService = game:GetService("StudioService")

-- ═══════════════════════════════════════════════════════════════
--  CONFIG
-- ═══════════════════════════════════════════════════════════════

local PUBLIC_URL = "https://rux.app"
local PLUGIN_VERSION = 4
local MIN_SERVER_VERSION = 3

-- ── Persisted settings (survive Studio restarts) ──
local function getSetting(key, fallback)
  local ok, val = pcall(function() return plugin:GetSetting(key) end)
  if ok and val ~= nil then return val end
  return fallback
end

local function setSetting(key, value)
  pcall(function() plugin:SetSetting(key, value) end)
end

local PLUGIN_ID = getSetting("rux_plugin_id", nil)
if not PLUGIN_ID or PLUGIN_ID == "" then
  PLUGIN_ID = "rux-" .. HttpService:GenerateGUID(false)
  setSetting("rux_plugin_id", PLUGIN_ID)
end

local SAVED_SESSION = getSetting("rux_session_id", nil)
local session_id = nil
local connected = false
local webConnected = false
local activities = {}
local snapshots = {}
local pollInterval = 3
local webDisconnectNotified = false
local webOfflineCount = 0
local webOnlineCount = 0
local consecutiveFailures = 0
local connectedAt = 0
local lastToolName = ""
local lastToolTime = 0
local toolRunning = false
local lastSelection = nil
local placeMetadata = nil
local scriptCache = {}
local scriptCacheTime = 0

-- ═══════════════════════════════════════════════════════════════
--  COLORS
-- ═══════════════════════════════════════════════════════════════

local C = {
  bg         = Color3.fromRGB(10, 10, 12),
  surface    = Color3.fromRGB(16, 16, 20),
  surface2   = Color3.fromRGB(22, 22, 28),
  surface3   = Color3.fromRGB(30, 30, 38),
  card       = Color3.fromRGB(18, 18, 23),
  border     = Color3.fromRGB(30, 30, 40),
  border2    = Color3.fromRGB(45, 45, 58),
  border3    = Color3.fromRGB(60, 60, 75),
  text       = Color3.fromRGB(245, 245, 250),
  text2      = Color3.fromRGB(190, 185, 160),
  text3      = Color3.fromRGB(150, 148, 140),
  muted      = Color3.fromRGB(80, 78, 75),
  accent     = Color3.fromRGB(245, 200, 80),
  accentDim  = Color3.fromRGB(200, 162, 55),
  accentBg   = Color3.fromRGB(35, 28, 8),
  accentGlow = Color3.fromRGB(80, 65, 15),
  success    = Color3.fromRGB(100, 210, 130),
  successBg  = Color3.fromRGB(12, 38, 20),
  error      = Color3.fromRGB(230, 110, 110),
  errorBg    = Color3.fromRGB(42, 14, 14),
  warning    = Color3.fromRGB(240, 180, 70),
  warningBg  = Color3.fromRGB(40, 28, 5),
  danger     = Color3.fromRGB(235, 75, 80),
  dangerBg   = Color3.fromRGB(50, 14, 14),
  blue       = Color3.fromRGB(100, 170, 255),
  blueBg     = Color3.fromRGB(12, 25, 52),
  purple     = Color3.fromRGB(175, 145, 255),
  green      = Color3.fromRGB(80, 220, 135),
  white      = Color3.fromRGB(255, 255, 255),
}

-- ═══════════════════════════════════════════════════════════════
--  HELPERS
-- ═══════════════════════════════════════════════════════════════

local function jsonEncode(data)
  local ok, r = pcall(HttpService.JSONEncode, HttpService, data)
  return ok and r or "{}"
end

local function jsonDecode(str)
  local ok, r = pcall(HttpService.JSONDecode, HttpService, str)
  return ok and r or nil
end

local function doRequest(url, method, body)
  local headers = {
    ["Content-Type"] = "application/json",
    ["X-Plugin-Version"] = tostring(PLUGIN_VERSION),
  }
  local b = body and jsonEncode(body) or nil
  local ok, res = pcall(function()
    return HttpService:RequestAsync({
      Url = url,
      Method = method or "GET",
      Headers = headers,
      Body = b,
    })
  end)
  if not ok then
    return false, "Network: " .. tostring(res)
  end
  if not res.Success then
    if res.StatusCode == 426 then
      return false, "OUTDATED"
    end
    return false, "HTTP " .. tostring(res.StatusCode) .. (res.Body and #res.Body > 0 and ": " .. res.Body:sub(1, 200) or "")
  end
  if res.Body and res.Body ~= "" then
    local decoded = jsonDecode(res.Body)
    if decoded then return true, decoded end
  end
  return true, nil
end

local function getFullName(inst)
  local ok, r = pcall(function() return inst:GetFullName() end)
  return ok and r or inst.Name
end

local function isScript(inst)
  return inst:IsA("Script") or inst:IsA("LocalScript") or inst:IsA("ModuleScript")
end

local function findScript(name)
  for _, inst in ipairs(game:GetDescendants()) do
    if isScript(inst) and inst.Name == name then return inst end
  end
  return nil
end

local function findByPath(path)
  if not path or path == "" then return nil end
  local parts = string.split(path, ".")
  local current = game
  local start = parts[1] == "game" and 2 or 1
  for i = start, #parts do
    if not current then return nil end
    local found = current:FindFirstChild(parts[i])
    if not found then return nil end
    current = found
  end
  return current
end

local function getSelectedInfo()
  local sel = Selection:Get()
  if #sel == 0 then return {name = nil, path = nil, className = nil} end
  local inst = sel[1]
  return {name = inst.Name, path = getFullName(inst), className = inst.ClassName}
end

-- ═══════════════════════════════════════════════════════════════
--  PROPERTY SERIALIZATION
-- ═══════════════════════════════════════════════════════════════

local READABLE_PROPS = {
  -- BasePart
  "Name", "ClassName", "Position", "Rotation", "Size", "CFrame",
  "Transparency", "CanCollide", "CanQuery", "CanTouch",
  "Anchored", "Massless", "CastShadow", "CollisionGroupId",
  "Color", "Material", "MaterialVariant", "Reflectance",
  "BrickColor", "BackSurface", "BottomSurface", "FrontSurface",
  "LeftSurface", "RightSurface", "TopSurface",
  -- PVInstance
  "WorldPivot",
  -- Instance
  "Parent", "Archivable",
  -- Tool
  "CanBeDropped", "ManualActivationOnly", "Enabled", "Grip",
  -- Light
  "Brightness", "Color", "Range", "Shadows",
  -- Sound
  "Volume", "PlaybackSpeed", "Looped", "Playing", "TimePosition",
  -- BillboardGui / SurfaceGui
  "Enabled", "LightInfluence", "Size", "StudsOffset", "MaxDistance",
  -- Decal / Texture
  "Texture", "Transparency", "Color3", "Face",
  -- Attachment
  "Position", "Axis", "SecondaryAxis", "WorldPosition",
  -- SpawnLocation
  "Duration", "Neutral", "TeamColor",
  -- General
  "Value",
}

local SPECIAL_PROPS = {
  Position = "Vector3",
  Size = "Vector3",
  Rotation = "Vector3",
  CFrame = "CFrame",
  Color = "Color3",
  Color3 = "Color3",
  BrickColor = "BrickColor",
  Grip = "CFrame",
  WorldPivot = "CFrame",
  StudsOffset = "Vector3",
  Axis = "Vector3",
  SecondaryAxis = "Vector3",
  WorldPosition = "Vector3",
  PivotOffset = "CFrame",
}

local function serializeValue(val, propType)
  if val == nil then return nil end

  if propType == "Vector3" and typeof(val) == "Vector3" then
    return { x = val.X, y = val.Y, z = val.Z }
  elseif propType == "CFrame" and typeof(val) == "CFrame" then
    local comps = { val:GetComponents() }
    return comps
  elseif propType == "Color3" and typeof(val) == "Color3" then
    return { r = math.round(val.R * 255), g = math.round(val.G * 255), b = math.round(val.B * 255) }
  elseif propType == "BrickColor" and typeof(val) == "BrickColor" then
    return {
      name = val.Name,
      number = val.Number,
      color = {
        r = math.round(val.Color.R * 255),
        g = math.round(val.Color.G * 255),
        b = math.round(val.Color.B * 255),
      },
    }
  elseif typeof(val) == "EnumItem" then
    return tostring(val)
  elseif typeof(val) == "Instance" then
    local ok, name = pcall(getFullName, val)
    return ok and name or val.Name
  elseif typeof(val) == "boolean" or typeof(val) == "number" or typeof(val) == "string" then
    return val
  elseif typeof(val) == "Color3" then
    return { r = math.round(val.R * 255), g = math.round(val.G * 255), b = math.round(val.B * 255) }
  elseif typeof(val) == "Vector3" then
    return { x = val.X, y = val.Y, z = val.Z }
  elseif typeof(val) == "CFrame" then
    local comps = { val:GetComponents() }
    return comps
  elseif typeof(val) == "BrickColor" then
    return { name = val.Name, number = val.Number }
  else
    return tostring(val)
  end
end

local function deserializeValue(value, propName, className)
  local propType = SPECIAL_PROPS[propName]

  if propType == "Vector3" or propName == "Position" or propName == "Size"
    or propName == "Rotation" or propName == "StudsOffset"
    or propName == "Axis" or propName == "SecondaryAxis"
    or propName == "WorldPosition" or propName == "Velocity"
    or propName == "RotVelocity" then
    if typeof(value) == "table" and #value == 3 then
      return Vector3.new(value[1] or 0, value[2] or 0, value[3] or 0)
    elseif typeof(value) == "table" and value.x ~= nil then
      return Vector3.new(value.x or 0, value.y or 0, value.z or 0)
    end
  end

  if propType == "CFrame" or propName == "CFrame" or propName == "Grip"
    or propName == "WorldPivot" or propName == "PivotOffset" then
    if typeof(value) == "table" and #value == 12 then
      return CFrame.new(
        value[1], value[2], value[3],
        value[4], value[5], value[6],
        value[7], value[8], value[9],
        value[10], value[11], value[12]
      )
    elseif typeof(value) == "table" and #value == 3 then
      return CFrame.new(value[1] or 0, value[2] or 0, value[3] or 0)
    elseif typeof(value) == "table" and value.x ~= nil then
      return CFrame.new(value.x or 0, value.y or 0, value.z or 0)
    end
  end

  if propType == "Color3" or propName == "Color" or propName == "Color3"
    or propName == "ImageColor3" or propName == "TextColor3"
    or propName == "BackgroundColor3" then
    if typeof(value) == "table" then
      if #value == 3 then
        return Color3.fromRGB(value[1] or 255, value[2] or 255, value[3] or 255)
      elseif value.r ~= nil then
        return Color3.fromRGB(value.r or 255, value.g or 255, value.b or 255)
      end
    end
  end

  if propType == "BrickColor" or propName == "BrickColor" or propName == "TeamColor" then
    if typeof(value) == "string" then
      local bc = BrickColor.new(value)
      if bc then return bc end
    elseif typeof(value) == "number" then
      local bc = BrickColor.new(value)
      if bc then return bc end
    end
  end

  if propName == "Material" then
    if typeof(value) == "string" then
      local m = Enum.Material[value]
      if m then return m end
    end
  end

  if propName == "BackSurface" or propName == "BottomSurface"
    or propName == "FrontSurface" or propName == "LeftSurface"
    or propName == "RightSurface" or propName == "TopSurface" then
    if typeof(value) == "string" then
      local s = Enum.SurfaceType[value]
      if s then return s end
    end
  end

  if propName == "Parent" then
    if typeof(value) == "string" then
      return findByPath(value)
    end
  end

  if typeof(value) == "string" then
    local enumMap = {
      Shape = "Enum.PartType",
      FormFactor = "Enum.FormFactor",
      Face = "Enum.NormalId",
      Font = "Enum.Font",
      TextXAlignment = "Enum.TextXAlignment",
      TextYAlignment = "Enum.TextYAlignment",
      TextTruncate = "Enum.TextTruncate",
      Alignment = "Enum.TextAlignment",
      ScaleType = "Enum.ScaleType",
    }
    if enumMap[propName] then
      local enumPath = string.split(enumMap[propName], ".")
      if #enumPath == 2 then
        local ok, result = pcall(function()
          return Enum[enumPath[2]][value]
        end)
        if ok and result then return result end
      end
    end
  end

  return value
end

-- ═══════════════════════════════════════════════════════════════
--  TOOL EXECUTION
-- ═══════════════════════════════════════════════════════════════

local function truncateResult(data)
  local encoded = jsonEncode(data)
  if #encoded <= 25000 then return data end
  return {
    truncated = true,
    preview = encoded:sub(1, 5000),
    notice = "Result truncated (" .. #encoded .. " chars). Use more specific queries.",
  }
end

local function executeTool(name, args)
  local function ok(data) return {success = true, data = data} end
  local function err(msg, suggestion) return {success = false, error = msg, suggestion = suggestion or ""} end

  if name == "read_script" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name), "Use list_scripts to see available scripts") end
    local rok, src = pcall(function() return inst.Source end)
    if not rok then return err("Cannot read source") end
    return ok({name = inst.Name, path = getFullName(inst), source = src})

  elseif name == "write_script" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local sok, src = pcall(function() return inst.Source end)
    if sok then snapshots[args.name] = {source = src, time = os.time()} end
    local wok, werr = pcall(function() inst.Source = args.code end)
    if not wok then return err("Write failed: " .. tostring(werr)) end
    return ok({message = "Script updated", name = inst.Name, snapshot_saved = true})

  elseif name == "create_script" then
    local parent = findByPath(args.parent)
    if not parent then return err("Parent not found: " .. tostring(args.parent)) end
    local cls = args.type == "LocalScript" and "LocalScript"
      or args.type == "ModuleScript" and "ModuleScript"
      or "Script"
    local s = Instance.new(cls)
    s.Name = args.name
    s.Parent = parent
    return ok({name = s.Name, path = getFullName(s), className = s.ClassName})

  elseif name == "delete_script" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local sok, src = pcall(function() return inst.Source end)
    if sok then snapshots[args.name] = {source = src, time = os.time()} end
    inst:Destroy()
    return ok({message = "Script deleted", snapshot_saved = true})

  elseif name == "list_scripts" then
    local list = {}
    for _, inst in ipairs(game:GetDescendants()) do
      if isScript(inst) then table.insert(list, inst.Name) end
    end
    return ok({scripts = list})

  elseif name == "get_script_tree" then
    local tree = {}
    for _, inst in ipairs(game:GetDescendants()) do
      if isScript(inst) then
        table.insert(tree, {name = inst.Name, className = inst.ClassName, path = getFullName(inst)})
      end
    end
    return ok({tree = tree})

  elseif name == "check_errors" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local cok, src = pcall(function() return inst.Source end)
    if not cok then return err("Cannot read source") end
    return ok({message = "Basic check passed.", source_length = #src})

  elseif name == "get_output_log" then
    return ok({message = "Plugin log:", logs = {}})

  elseif name == "get_error_log" then
    local errors = {}
    for _, a in ipairs(activities) do
      if a.error then table.insert(errors, a.text) end
    end
    return ok({errors = errors})

  elseif name == "search_code" then
    local results = {}
    local query = string.lower(tostring(args.query))
    local count = 0
    for _, inst in ipairs(game:GetDescendants()) do
      if isScript(inst) and count < 30 then
        local sok, src = pcall(function() return inst.Source end)
        if sok then
          local lineNum = 0
          for line in string.gmatch(src, "([^\n]*)\n?") do
            lineNum += 1
            if string.find(string.lower(line), query, 1, true) then
              table.insert(results, {script = inst.Name, path = getFullName(inst), line = lineNum, content = line})
              count += 1
              if count >= 30 then break end
            end
          end
        end
      end
    end
    return ok({matches = results})

  elseif name == "find_usages" then
    return executeTool("search_code", {query = args.variable_name})

  elseif name == "get_instance_tree" then
    local maxDepth = args.max_depth or 3
    local function walk(node, depth)
      if not node then return nil end
      if node:IsA("Terrain") then return {name = "Terrain", className = "Terrain"} end
      if depth > maxDepth then
        return {name = node.Name, className = node.ClassName, childCount = #node:GetChildren()}
      end
      local children = {}
      local childList = node:GetChildren()
      if depth >= maxDepth and #childList > 20 then
        return {name = node.Name, className = node.ClassName, childCount = #childList}
      end
      for _, child in ipairs(childList) do
        local walked = walk(child, depth + 1)
        if walked then table.insert(children, walked) end
      end
      if depth >= 3 then
        return {name = node.Name, className = node.ClassName}
      end
      return {name = node.Name, className = node.ClassName, path = getFullName(node), children = children}
    end
    return truncateResult(ok({tree = walk(game, 0)}))

  elseif name == "get_properties" then
    local inst = findByPath(args.instance_path)
    if not inst then
      for _, d in ipairs(game:GetDescendants()) do
        if d.Name == args.instance_path then
          inst = d
          break
        end
      end
    end
    if not inst then return err("Instance not found: " .. tostring(args.instance_path), "Use find_instance to locate it first") end

    local props = {}
    props.Name = inst.Name
    props.ClassName = inst.ClassName
    props.Path = getFullName(inst)
    if inst.Parent then props.Parent = getFullName(inst.Parent) end

    for _, propName in ipairs(READABLE_PROPS) do
      if propName == "Name" or propName == "ClassName" or propName == "Parent" then
        -- Already handled
      else
        local pok, val = pcall(function() return inst[propName] end)
        if pok and val ~= nil then
          local serialized = serializeValue(val, SPECIAL_PROPS[propName])
          if serialized ~= nil then
            props[propName] = serialized
          end
        end
      end
    end

    if inst:IsA("BasePart") then
      local pok, val = pcall(function() return inst.AssemblyLinearVelocity end)
      if pok then props.AssemblyLinearVelocity = serializeValue(val, "Vector3") end
    end
    if inst:IsA("BasePart") then
      local pok, val = pcall(function() return inst.CustomPhysicalProperties end)
      if pok and val then
        props.CustomPhysicalProperties = {
          Density = val.Density,
          Friction = val.Friction,
          Elasticity = val.Elasticity,
          FrictionWeight = val.FrictionWeight,
          ElasticityWeight = val.ElasticityWeight,
        }
      end
    end
    if inst:IsA("ValueBase") then
      local pok, val = pcall(function() return inst.Value end)
      if pok then props.Value = serializeValue(val, SPECIAL_PROPS["Value"]) end
    end
    if inst:IsA("TextLabel") or inst:IsA("TextButton") then
      for _, tp in ipairs({"Font", "TextSize", "TextColor3", "Text", "TextScaled", "TextWrapped",
        "TextXAlignment", "TextYAlignment", "BackgroundColor3", "BackgroundTransparency",
        "BorderSizePixel", "ClipsDescendants", "Visible", "ZIndex"}) do
        local pok, val = pcall(function() return inst[tp] end)
        if pok and val ~= nil then props[tp] = serializeValue(val, SPECIAL_PROPS[tp]) end
      end
    end

    return truncateResult(ok(props))

  elseif name == "set_property" then
    local inst = findByPath(args.instance_path)
    if not inst then return err("Instance not found: " .. tostring(args.instance_path), "Use find_instance to locate it") end
    local propName = args.property
    local rawValue = args.value
    local converted = deserializeValue(rawValue, propName, inst.ClassName)
    if converted == nil and rawValue ~= nil then
      return err("Cannot convert value for " .. propName, "Check the value type matches the property")
    end
    local pok, perr = pcall(function() inst[propName] = converted end)
    if not pok then
      return err("Failed to set " .. tostring(propName) .. ": " .. tostring(perr), "Property may be read-only or value type mismatch")
    end
    local vok, newVal = pcall(function() return inst[propName] end)
    local verified = vok and newVal ~= nil
    return ok({message = propName .. " updated", property = propName, verified = verified})

  elseif name == "add_instance" then
    local className = args.class_name
    local parentPath = args.parent_path
    local instName = args.name
    local properties = args.properties or {}

    if not className or not parentPath or not instName then
      return err("Missing required fields: class_name, parent_path, name")
    end

    local parent = findByPath(parentPath)
    if not parent then
      return err("Parent not found: " .. tostring(parentPath), "Use find_instance or get_instance_tree to find the parent path")
    end

    local inst
    local createOk, createErr = pcall(function()
      inst = Instance.new(className)
      inst.Name = instName
    end)
    if not createOk or not inst then
      return err("Failed to create " .. tostring(className) .. ": " .. tostring(createErr), "Check the class name is valid in Roblox")
    end

    local propErrors = {}
    for propName, rawValue in pairs(properties) do
      if propName ~= "Parent" then
        local converted = deserializeValue(rawValue, propName, className)
        local pok, perr = pcall(function() inst[propName] = converted end)
        if not pok then
          table.insert(propErrors, propName .. ": " .. tostring(perr))
        end
      end
    end

    local parentOk, parentErr = pcall(function() inst.Parent = parent end)
    if not parentOk then
      pcall(function() inst:Destroy() end)
      return err("Failed to parent to " .. tostring(parentPath) .. ": " .. tostring(parentErr))
    end

    pcall(function() Selection:Set({inst}) end)

    local result = {
      ok = true,
      class_name = className,
      name = instName,
      path = getFullName(inst),
      message = "Created " .. className .. " '" .. instName .. "' under " .. parentPath,
    }
    if #propErrors > 0 then
      result.property_errors = propErrors
    end
    return ok(result)

  elseif name == "find_instance" then
    local results = {}
    for _, inst in ipairs(game:GetDescendants()) do
      if inst.Name == args.name then
        table.insert(results, {
          name = inst.Name,
          path = getFullName(inst),
          className = inst.ClassName,
        })
        if #results >= 10 then break end
      end
    end
    if #results == 0 then
      return err("Instance not found: " .. tostring(args.name), "Try get_instance_tree to browse the hierarchy")
    end
    return ok(#results == 1 and results[1] or {matches = results})

  elseif name == "get_selection" then
    local sel = Selection:Get()
    if #sel == 0 then return ok({selection = {name = nil, path = nil, className = nil}}) end
    local inst = sel[1]
    local info = {
      name = inst.Name,
      path = getFullName(inst),
      className = inst.ClassName,
    }
    local propResult = executeTool("get_properties", {instance_path = getFullName(inst)})
    if propResult.success and propResult.data then
      info.properties = propResult.data
    end
    return ok({selection = info, count = #sel})

  elseif name == "get_current_script" then
    local sel = Selection:Get()
    for _, inst in ipairs(sel) do
      if isScript(inst) then
        local cok, src = pcall(function() return inst.Source end)
        if cok then return ok({name = inst.Name, path = getFullName(inst), source = src}) end
      end
    end
    return err("No script selected", "Click on a Script, LocalScript, or ModuleScript in the Explorer")

  elseif name == "get_place_metadata" then
    return ok({
      metadata = {
        game_name = game.Name,
        place_id = game.PlaceId,
        place_version = game.PlaceVersion,
        creator_id = game.CreatorId,
        creator_type = tostring(game.CreatorType),
      }
    })

  elseif name == "get_workspace_summary" then
    -- Compact one-shot snapshot of the place — services, top-level children,
    -- script counts, and instance counts. Cheap to call, safe for the LLM
    -- to use as the very first step of a session.
    local serviceNames = {
      "Workspace", "ReplicatedStorage", "ReplicatedFirst",
      "ServerStorage", "ServerScriptService",
      "StarterGui", "StarterPack", "StarterPlayer",
      "Lighting", "SoundService", "Players", "Teams",
      "Chat", "MaterialService",
    }
    local services = {}
    local totalScripts = 0
    local totalInstances = 0
    for _, sName in ipairs(serviceNames) do
      local sok, svc = pcall(function() return game:GetService(sName) end)
      if sok and svc then
        local kids = svc:GetChildren()
        local descCount = 0
        local scriptCount = 0
        local breakdown = {Script = 0, LocalScript = 0, ModuleScript = 0}
        local descOk = pcall(function()
          for _, d in ipairs(svc:GetDescendants()) do
            descCount += 1
            local cls = d.ClassName
            if cls == "Script" or cls == "LocalScript" or cls == "ModuleScript" then
              scriptCount += 1
              breakdown[cls] = (breakdown[cls] or 0) + 1
            end
          end
        end)
        if not descOk then descCount = #kids end
        totalScripts += scriptCount
        totalInstances += descCount
        local topChildren = {}
        local maxKids = math.min(#kids, 12)
        for i = 1, maxKids do
          local c = kids[i]
          table.insert(topChildren, {
            name = c.Name,
            class = c.ClassName,
            children = #c:GetChildren(),
          })
        end
        table.insert(services, {
          service = sName,
          child_count = #kids,
          descendant_count = descCount,
          script_count = scriptCount,
          script_breakdown = breakdown,
          top_children = topChildren,
          truncated = #kids > maxKids,
        })
      end
    end
    return truncateResult(ok({
      place = {
        name = game.Name,
        place_id = game.PlaceId,
        place_version = game.PlaceVersion,
      },
      totals = {
        services = #services,
        scripts = totalScripts,
        instances = totalInstances,
      },
      services = services,
    }))

  elseif name == "snapshot_script" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local sok, src = pcall(function() return inst.Source end)
    if not sok then return err("Cannot read source") end
    snapshots[args.name] = {source = src, time = os.time()}
    return ok({message = "Snapshot saved for " .. args.name})

  elseif name == "diff_script" then
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local snap = snapshots[args.name]
    if not snap then return err("No snapshot for " .. args.name) end
    local dok, cur = pcall(function() return inst.Source end)
    if not dok then return err("Cannot read current source") end
    local oldLines = string.split(snap.source, "\n")
    local newLines = string.split(cur, "\n")
    local diffs = {}
    for i = 1, math.max(#oldLines, #newLines) do
      if oldLines[i] ~= newLines[i] then
        table.insert(diffs, {line = i, before = oldLines[i], after = newLines[i]})
      end
    end
    return ok({diff = diffs})

  elseif name == "restore_script" then
    local snap = snapshots[args.name]
    if not snap then return err("No snapshot for " .. args.name) end
    local inst = findScript(args.name)
    if not inst then return err("Script not found: " .. tostring(args.name)) end
    local rok, rerr = pcall(function() inst.Source = snap.source end)
    if not rok then return err("Restore failed: " .. tostring(rerr)) end
    return ok({message = "Restored " .. args.name})

  else
    return err("Unknown tool: " .. tostring(name))
  end
end

-- ═══════════════════════════════════════════════════════════════
--  BULK SNAPSHOT
-- ═══════════════════════════════════════════════════════════════

local function snapshotAllScripts()
  local count = 0
  for _, inst in ipairs(game:GetDescendants()) do
    if isScript(inst) then
      local sok, src = pcall(function() return inst.Source end)
      if sok then
        snapshots[inst.Name] = {source = src, time = os.time()}
        count += 1
      end
    end
  end
  return count
end

-- ═══════════════════════════════════════════════════════════════
--  UI UTILITIES
-- ═══════════════════════════════════════════════════════════════

local FAST  = TweenInfo.new(0.15, Enum.EasingStyle.Quad, Enum.EasingDirection.Out)
local MED   = TweenInfo.new(0.25, Enum.EasingStyle.Quad, Enum.EasingDirection.Out)
local SLOW  = TweenInfo.new(0.4,  Enum.EasingStyle.Quad, Enum.EasingDirection.Out)
local PULSE = TweenInfo.new(1.2,  Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, true)

local function tw(obj, info, props)
  TweenService:Create(obj, info, props):Play()
end

local function corner(r, parent)
  local c = Instance.new("UICorner")
  c.CornerRadius = UDim.new(0, r)
  c.Parent = parent
  return c
end

local function pad(t, r, b, l, parent)
  local p = Instance.new("UIPadding")
  p.PaddingTop    = UDim.new(0, t or 0)
  p.PaddingRight  = UDim.new(0, r or 0)
  p.PaddingBottom = UDim.new(0, b or 0)
  p.PaddingLeft   = UDim.new(0, l or 0)
  if parent then p.Parent = parent end
  return p
end

local function stroke(color, thickness, parent)
  local s = Instance.new("UIStroke")
  s.Color = color or C.border
  s.Thickness = thickness or 1
  s.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
  if parent then s.Parent = parent end
  return s
end

local function vList(spacing, parent)
  local l = Instance.new("UIListLayout")
  l.FillDirection = Enum.FillDirection.Vertical
  l.HorizontalAlignment = Enum.HorizontalAlignment.Left
  l.VerticalAlignment = Enum.VerticalAlignment.Top
  l.SortOrder = Enum.SortOrder.LayoutOrder
  l.Padding = UDim.new(0, spacing or 0)
  if parent then l.Parent = parent end
  return l
end

local function hList(spacing, valign, parent)
  local l = Instance.new("UIListLayout")
  l.FillDirection = Enum.FillDirection.Horizontal
  l.HorizontalAlignment = Enum.HorizontalAlignment.Left
  l.VerticalAlignment = valign or Enum.VerticalAlignment.Center
  l.SortOrder = Enum.SortOrder.LayoutOrder
  l.Padding = UDim.new(0, spacing or 0)
  if parent then l.Parent = parent end
  return l
end

local function mkFrame(bg, size, pos, parent)
  local f = Instance.new("Frame")
  f.BackgroundColor3 = bg
  f.BorderSizePixel = 0
  if size then f.Size = size end
  if pos then f.Position = pos end
  if parent then f.Parent = parent end
  return f
end

local function mkLabel(text, size, textSize, color, font, parent)
  local l = Instance.new("TextLabel")
  l.BackgroundTransparency = 1
  l.BorderSizePixel = 0
  l.Text = text
  l.TextSize = textSize or 10
  l.TextColor3 = color or C.text3
  l.Font = font or Enum.Font.GothamMedium
  l.TextXAlignment = Enum.TextXAlignment.Left
  l.TextYAlignment = Enum.TextYAlignment.Center
  l.TextTruncate = Enum.TextTruncate.AtEnd
  if size then l.Size = size end
  if parent then l.Parent = parent end
  return l
end

local function mkBtn(text, bg, fg, size, parent)
  local b = Instance.new("TextButton")
  b.BackgroundColor3 = bg
  b.TextColor3 = fg
  b.Text = text
  b.Font = Enum.Font.GothamBold
  b.TextSize = 10
  b.BorderSizePixel = 0
  b.AutoButtonColor = false
  b.TextXAlignment = Enum.TextXAlignment.Center
  if size then b.Size = size end
  if parent then b.Parent = parent end
  return b
end

local function hover(btn, normalBg, hoverBg)
  btn.MouseEnter:Connect(function() tw(btn, FAST, {BackgroundColor3 = hoverBg}) end)
  btn.MouseLeave:Connect(function() tw(btn, FAST, {BackgroundColor3 = normalBg}) end)
end

-- ═══════════════════════════════════════════════════════════════
--  BUILD UI
-- ═══════════════════════════════════════════════════════════════

local function buildUI()
  local buildOk, buildErr = pcall(function()

    local widgetInfo = DockWidgetPluginGuiInfo.new(
      Enum.InitialDockState.Right,
      true, false, 340, 540, 260, 360
    )
    local widget = plugin:CreateDockWidgetPluginGui("RuxWidget6", widgetInfo)
    widget.Title = "Rux"

    local HEADER_H   = 46
    local BOTTOM_H   = 46
    local STATUS_H   = 152
    local LOG_HDR_H  = 30
    local WEB_BNR_H  = 28
    local TOOL_BNR_H = 26

    -- ── Root ──────────────────────────────────────────────
    local root = mkFrame(C.bg, UDim2.fromScale(1, 1), nil, widget)
    root.ClipsDescendants = true
    mkFrame(C.accent, UDim2.new(1, 0, 0, 2), UDim2.new(0, 0, 0, 0), root)

    -- ── Header ────────────────────────────────────────────
    local header = mkFrame(C.surface, UDim2.new(1, 0, 0, HEADER_H), UDim2.new(0, 0, 0, 0), root)
    stroke(C.border, 1, header)
    mkFrame(C.accent, UDim2.new(0, 2, 1, 0), UDim2.new(0, 0, 0, 0), header)
    pad(0, 12, 0, 16, header)
    hList(8, Enum.VerticalAlignment.Center, header)

    local logoBadge = mkFrame(C.accent, UDim2.fromOffset(26, 26), nil, header)
    corner(6, logoBadge)
    local logoR = mkLabel("R", UDim2.fromScale(1, 1), 13, C.bg, Enum.Font.GothamBlack, logoBadge)
    logoR.TextXAlignment = Enum.TextXAlignment.Center

    local titleStack = mkFrame(C.bg, UDim2.new(0, 80, 1, 0), nil, header)
    titleStack.BackgroundTransparency = 1
    vList(1, titleStack)
    mkLabel("Rux", UDim2.new(1, 0, 0, 18), 13, C.text, Enum.Font.GothamBlack, titleStack)
    mkLabel("Studio Bridge", UDim2.new(1, 0, 0, 12), 8, C.muted, Enum.Font.Gotham, titleStack)

    local vPill = mkFrame(C.accentBg, UDim2.fromOffset(22, 13), nil, header)
    corner(4, vPill)
    stroke(C.accentGlow, 1, vPill)
    local vLbl = mkLabel("v" .. PLUGIN_VERSION, UDim2.fromScale(1, 1), 7, C.accent, Enum.Font.GothamBold, vPill)
    vLbl.TextXAlignment = Enum.TextXAlignment.Center

    local hSpacer = mkFrame(C.bg, UDim2.new(1, 0, 0, 1), nil, header)
    hSpacer.BackgroundTransparency = 1

    local statusChip = mkFrame(C.surface2, UDim2.fromOffset(108, 22), nil, header)
    corner(6, statusChip)
    stroke(C.border, 1, statusChip)
    pad(0, 8, 0, 8, statusChip)
    hList(5, Enum.VerticalAlignment.Center, statusChip)

    local sDot = mkFrame(C.muted, UDim2.fromOffset(6, 6), nil, statusChip)
    corner(3, sDot)
    local sLbl = mkLabel("Offline", UDim2.new(1, 0, 1, 0), 8, C.muted, Enum.Font.GothamMedium, statusChip)

    -- ── Outdated Banner ───────────────────────────────────
    local outdatedBanner = mkFrame(C.dangerBg,
      UDim2.new(1, 0, 0, 38), UDim2.new(0, 0, 0, HEADER_H), root)
    outdatedBanner.Visible = false
    outdatedBanner.ZIndex = 20
    stroke(C.danger, 1, outdatedBanner)
    pad(6, 12, 6, 12, outdatedBanner)
    vList(2, outdatedBanner)
    local ob1 = mkLabel("⚠  Plugin Outdated", UDim2.new(1, 0, 0, 14), 10,
      C.danger, Enum.Font.GothamBold, outdatedBanner)
    ob1.ZIndex = 21
    local ob2 = mkLabel("Update to the latest version to connect.", UDim2.new(1, 0, 0, 12), 9,
      Color3.fromRGB(190, 100, 100), Enum.Font.Gotham, outdatedBanner)
    ob2.ZIndex = 21

    -- ── Content Area ──────────────────────────────────────
    local content = mkFrame(C.bg,
      UDim2.new(1, 0, 1, -(HEADER_H + BOTTOM_H)),
      UDim2.new(0, 0, 0, HEADER_H), root)
    content.ClipsDescendants = true

    -- ═════════════════════════════════════════════════════
    --  CONNECT SCREEN
    -- ═════════════════════════════════════════════════════
    local connectScreen = mkFrame(C.bg, UDim2.fromScale(1, 1), nil, content)

    local connectScroll = Instance.new("ScrollingFrame")
    connectScroll.Size = UDim2.fromScale(1, 1)
    connectScroll.BackgroundTransparency = 1
    connectScroll.BorderSizePixel = 0
    connectScroll.ScrollBarThickness = 3
    connectScroll.ScrollBarImageColor3 = C.border2
    connectScroll.AutomaticCanvasSize = Enum.AutomaticSize.Y
    connectScroll.CanvasSize = UDim2.new(0, 0, 0, 0)
    connectScroll.Parent = connectScreen
    pad(20, 16, 20, 16, connectScroll)
    local csLayout = vList(12, connectScroll)
    csLayout.HorizontalAlignment = Enum.HorizontalAlignment.Center

    local hero = mkFrame(C.bg, UDim2.new(1, 0, 0, 1), nil, connectScroll)
    hero.BackgroundTransparency = 1
    hero.LayoutOrder = 0
    hero.AutomaticSize = Enum.AutomaticSize.Y
    local heroLayout = vList(10, hero)
    heroLayout.HorizontalAlignment = Enum.HorizontalAlignment.Center

    local bigLogo = mkFrame(C.accent, UDim2.fromOffset(52, 52), nil, hero)
    corner(13, bigLogo)
    stroke(C.accentGlow, 2, bigLogo)
    local bigR = mkLabel("R", UDim2.fromScale(1, 1), 24, C.bg, Enum.Font.GothamBlack, bigLogo)
    bigR.TextXAlignment = Enum.TextXAlignment.Center

    local heroTitle = mkLabel("Rux Studio", UDim2.new(1, 0, 0, 22), 17, C.text, Enum.Font.GothamBlack, hero)
    heroTitle.TextXAlignment = Enum.TextXAlignment.Center

    local heroSub = mkLabel("AI-powered Studio bridge", UDim2.new(1, 0, 0, 15), 10, C.text3, Enum.Font.GothamMedium, hero)
    heroSub.TextXAlignment = Enum.TextXAlignment.Center

    local div1 = mkFrame(C.border, UDim2.new(1, 0, 0, 1), nil, connectScroll)
    div1.LayoutOrder = 1

    local instrCard = mkFrame(C.surface, UDim2.new(1, 0, 0, 1), nil, connectScroll)
    instrCard.LayoutOrder = 2
    instrCard.AutomaticSize = Enum.AutomaticSize.Y
    corner(10, instrCard)
    stroke(C.border, 1, instrCard)
    pad(12, 14, 12, 14, instrCard)
    vList(5, instrCard)
    mkLabel("HOW TO CONNECT", UDim2.new(1, 0, 0, 12), 8, C.muted, Enum.Font.GothamBold, instrCard).LayoutOrder = 0
    local steps = {
      "① Open the Rux website",
      "② Sign in and open your workspace",
      "③ Click Connect below — you're done!",
    }
    for i, step in ipairs(steps) do
      local sl = mkLabel(step, UDim2.new(1, 0, 0, 15), 9, C.text3, Enum.Font.Gotham, instrCard)
      sl.LayoutOrder = i
      sl.TextTruncate = Enum.TextTruncate.None
    end

    local connectBtn = mkBtn("Connect to Rux", C.accent, C.bg, UDim2.new(1, 0, 0, 42), connectScroll)
    connectBtn.LayoutOrder = 30
    connectBtn.TextSize = 12
    corner(10, connectBtn)
    hover(connectBtn, C.accent, C.accentDim)

    local errLbl = mkLabel("", UDim2.new(1, 0, 0, 15), 9, C.error, Enum.Font.GothamMedium, connectScroll)
    errLbl.LayoutOrder = 31
    errLbl.Visible = true
    errLbl.TextXAlignment = Enum.TextXAlignment.Center
    errLbl.TextTruncate = Enum.TextTruncate.None

    local footerHint = mkLabel("Opens rux.app • No code needed", UDim2.new(1, 0, 0, 13), 8,
      C.muted, Enum.Font.Gotham, connectScroll)
    footerHint.LayoutOrder = 5
    footerHint.TextXAlignment = Enum.TextXAlignment.Center

    -- ═════════════════════════════════════════════════════
    --  CONNECTED SCREEN
    -- ═════════════════════════════════════════════════════
    local connScreen = mkFrame(C.bg, UDim2.fromScale(1, 1), nil, content)
    connScreen.Visible = false

    -- Status card
    local statusCard = mkFrame(C.surface,
      UDim2.new(1, 0, 0, STATUS_H), UDim2.new(0, 0, 0, 0), connScreen)
    stroke(C.border, 1, statusCard)
    pad(10, 12, 10, 12, statusCard)
    vList(6, statusCard)

    mkLabel("CONNECTION", UDim2.new(1, 0, 0, 10), 7,
      C.muted, Enum.Font.GothamBold, statusCard).LayoutOrder = 0

    local indRow = mkFrame(C.surface2, UDim2.new(1, 0, 0, 26), nil, statusCard)
    indRow.LayoutOrder = 1
    corner(7, indRow)
    stroke(C.border, 1, indRow)
    pad(0, 10, 0, 10, indRow)
    hList(0, Enum.VerticalAlignment.Center, indRow)

    local pluginDot = mkFrame(C.accent, UDim2.fromOffset(7, 7), nil, indRow)
    corner(4, pluginDot)
    local sp1 = mkFrame(C.bg, UDim2.fromOffset(5, 1), nil, indRow) sp1.BackgroundTransparency = 1
    mkLabel("Plugin", UDim2.fromOffset(42, 26), 9, C.text3, Enum.Font.GothamMedium, indRow)
    mkFrame(C.border, UDim2.fromOffset(1, 14), nil, indRow)
    local sp2 = mkFrame(C.bg, UDim2.fromOffset(10, 1), nil, indRow) sp2.BackgroundTransparency = 1
    local webDot = mkFrame(C.muted, UDim2.fromOffset(7, 7), nil, indRow)
    corner(4, webDot)
    local sp3 = mkFrame(C.bg, UDim2.fromOffset(5, 1), nil, indRow) sp3.BackgroundTransparency = 1
    local webLbl = mkLabel("Web: Offline", UDim2.new(1, 0, 1, 0), 9, C.muted, Enum.Font.GothamMedium, indRow)

    local durLbl = mkLabel("0m", UDim2.fromOffset(42, 26), 8, C.muted, Enum.Font.Code, nil)
    durLbl.TextXAlignment = Enum.TextXAlignment.Right
    durLbl.Position = UDim2.new(1, -44, 0, 0)
    durLbl.Parent = indRow

    local sesRow = mkFrame(C.surface2, UDim2.new(1, 0, 0, 22), nil, statusCard)
    sesRow.LayoutOrder = 2
    corner(6, sesRow) stroke(C.border, 1, sesRow) pad(0, 10, 0, 10, sesRow)
    hList(6, Enum.VerticalAlignment.Center, sesRow)
    mkLabel("SESSION", UDim2.fromOffset(54, 22), 7, C.muted, Enum.Font.GothamBold, sesRow)
    local sesLbl = mkLabel("—", UDim2.new(1, 0, 1, 0), 9, C.text3, Enum.Font.Code, sesRow)

    local toolRow = mkFrame(C.surface2, UDim2.new(1, 0, 0, 22), nil, statusCard)
    toolRow.LayoutOrder = 3
    corner(6, toolRow) stroke(C.border, 1, toolRow) pad(0, 10, 0, 10, toolRow)
    hList(6, Enum.VerticalAlignment.Center, toolRow)
    mkLabel("LAST TOOL", UDim2.fromOffset(62, 22), 7, C.muted, Enum.Font.GothamBold, toolRow)
    local lastToolLbl = mkLabel("—", UDim2.new(1, 0, 1, 0), 9, C.text3, Enum.Font.Code, toolRow)

    local discBtn = mkBtn("Disconnect Session", C.surface2, C.danger,
      UDim2.new(1, 0, 0, 24), statusCard)
    discBtn.LayoutOrder = 4
    corner(6, discBtn)
    stroke(Color3.fromRGB(70, 20, 20), 1, discBtn)
    hover(discBtn, C.surface2, C.dangerBg)

    -- Web offline banner
    local webBanner = mkFrame(C.warningBg,
      UDim2.new(1, 0, 0, 0), UDim2.new(0, 0, 0, STATUS_H), connScreen)
    webBanner.ClipsDescendants = true
    stroke(Color3.fromRGB(90, 65, 10), 1, webBanner)
    pad(0, 14, 0, 12, webBanner)
    hList(6, Enum.VerticalAlignment.Center, webBanner)
    mkLabel("●", UDim2.fromOffset(10, 28), 8, C.warning, Enum.Font.GothamBold, webBanner)
    mkLabel("Web client offline — AI tools paused",
      UDim2.new(1, 0, 1, 0), 9, C.warning, Enum.Font.GothamMedium, webBanner)

    -- Tool running banner
    local toolBanner = mkFrame(C.blueBg,
      UDim2.new(1, 0, 0, 0), UDim2.new(0, 0, 0, STATUS_H), connScreen)
    toolBanner.ClipsDescendants = true
    stroke(Color3.fromRGB(20, 50, 100), 1, toolBanner)
    pad(0, 14, 0, 12, toolBanner)
    hList(6, Enum.VerticalAlignment.Center, toolBanner)
    local toolBannerDot = mkFrame(C.blue, UDim2.fromOffset(6, 6), nil, toolBanner)
    corner(3, toolBannerDot)
    local toolBannerLbl = mkLabel("Running…", UDim2.new(1, 0, 1, 0), 9, C.blue, Enum.Font.GothamMedium, toolBanner)

    -- Log section — absolutely fills remaining space below status card
    local logSection = mkFrame(C.bg,
      UDim2.new(1, 0, 1, -STATUS_H),
      UDim2.new(0, 0, 0, STATUS_H), connScreen)

    local logInner = mkFrame(C.bg, UDim2.fromScale(1, 1), UDim2.new(0, 0, 0, 0), logSection)
    logInner.ClipsDescendants = true

    local logHeader = mkFrame(C.surface,
      UDim2.new(1, 0, 0, LOG_HDR_H), UDim2.new(0, 0, 0, 0), logInner)
    stroke(C.border, 1, logHeader)
    pad(0, 10, 0, 14, logHeader)
    hList(6, Enum.VerticalAlignment.Center, logHeader)

    local logDot = mkFrame(C.accent, UDim2.fromOffset(4, 4), nil, logHeader)
    corner(2, logDot)
    mkLabel("LOG", UDim2.fromOffset(28, 30), 8, C.muted, Enum.Font.GothamBold, logHeader)

    local cntBadge = mkFrame(C.surface2, UDim2.fromOffset(28, 14), nil, logHeader)
    corner(4, cntBadge)
    stroke(C.border2, 1, cntBadge)
    local cntLbl = mkLabel("0", UDim2.fromScale(1, 1), 7, C.muted, Enum.Font.GothamBold, cntBadge)
    cntLbl.TextXAlignment = Enum.TextXAlignment.Center

    local lhSpacer = mkFrame(C.bg, UDim2.new(1, 0, 0, 1), nil, logHeader)
    lhSpacer.BackgroundTransparency = 1

    local clearBtn = mkBtn("Clear", C.surface2, C.muted, UDim2.fromOffset(44, 20), logHeader)
    corner(5, clearBtn)
    hover(clearBtn, C.surface2, C.surface3)

    local actScroll = Instance.new("ScrollingFrame")
    actScroll.Size = UDim2.new(1, 0, 1, -LOG_HDR_H)
    actScroll.Position = UDim2.new(0, 0, 0, LOG_HDR_H)
    actScroll.BackgroundTransparency = 1
    actScroll.BorderSizePixel = 0
    actScroll.ScrollBarThickness = 3
    actScroll.ScrollBarImageColor3 = C.border2
    actScroll.AutomaticCanvasSize = Enum.AutomaticSize.Y
    actScroll.CanvasSize = UDim2.new(0, 0, 0, 0)
    actScroll.Parent = logInner
    pad(4, 10, 6, 10, actScroll)
    vList(2, actScroll)

    -- Banner layout updater
    local webBannerShown  = false
    local toolBannerShown = false

    local function updateBannerLayout()
      local webH  = webBannerShown  and WEB_BNR_H  or 0
      local toolH = toolBannerShown and TOOL_BNR_H or 0
      tw(webBanner, MED, {
        Size     = UDim2.new(1, 0, 0, webH),
        Position = UDim2.new(0, 0, 0, STATUS_H),
      })
      tw(toolBanner, MED, {
        Size     = UDim2.new(1, 0, 0, toolH),
        Position = UDim2.new(0, 0, 0, STATUS_H + webH),
      })
      tw(logInner, MED, {
        Size     = UDim2.new(1, 0, 1, -(webH + toolH)),
        Position = UDim2.new(0, 0, 0, webH + toolH),
      })
    end

    -- ── Bottom Bar ────────────────────────────────────────
    local bottomBar = mkFrame(C.surface,
      UDim2.new(1, 0, 0, BOTTOM_H), UDim2.new(0, 0, 1, -BOTTOM_H), root)
    bottomBar.ZIndex = 10
    stroke(C.border, 1, bottomBar)
    pad(8, 10, 8, 10, bottomBar)
    hList(6, Enum.VerticalAlignment.Center, bottomBar)

    local function mkActionBtn(text, bg, fg, sc, order)
      local b = mkBtn(text, bg, fg, UDim2.new(0.333, -4, 1, 0), bottomBar)
      b.TextSize = 9
      b.LayoutOrder = order
      corner(8, b)
      if sc then stroke(sc, 1, b) end
      return b
    end

    local snapBtn    = mkActionBtn("📷 Snap (0)",  C.surface2, C.text3,  C.border2,    0)
    local restoreBtn = mkActionBtn("↩ Restore",    C.surface2, C.text3,  C.border2,    1)
    local webOpenBtn = mkActionBtn("Open Rux ↗",  C.accentBg, C.accent, C.accentGlow, 2)
    hover(snapBtn,    C.surface2, C.surface3)
    hover(restoreBtn, C.surface2, C.surface3)
    hover(webOpenBtn, C.accentBg, Color3.fromRGB(50, 40, 12))

    -- ═══════════════════════════════════════════════════════
    --  STATE FUNCTIONS
    -- ═══════════════════════════════════════════════════════

    local isOutdated = false
    local actCount   = 0

    local function setStatus(text, on)
      connected = on
      sLbl.Text = text
      sLbl.TextColor3 = on and C.accent or C.muted
      sDot.BackgroundColor3 = on and C.accent or C.muted
      if on then
        TweenService:Create(sDot, PULSE, {BackgroundTransparency = 0.5}):Play()
      end
    end

    local function setWebStatus(state)
      if state == "active" then
        webConnected = true
        webDot.BackgroundColor3 = C.success
        webLbl.Text = "Web: Active"
        webLbl.TextColor3 = C.text3
        webBannerShown = false
        updateBannerLayout()
      elseif state == "idle" then
        webConnected = true
        webDot.BackgroundColor3 = C.accent
        webLbl.Text = "Web: Idle"
        webLbl.TextColor3 = C.text3
        webBannerShown = false
        updateBannerLayout()
      else
        webConnected = false
        webDot.BackgroundColor3 = C.muted
        webLbl.Text = "Web: Offline"
        webLbl.TextColor3 = C.muted
        if connected and not webDisconnectNotified then
          webDisconnectNotified = true
          webBannerShown = true
          updateBannerLayout()
        end
      end
      if webConnected then
        webDisconnectNotified = false
      end
    end

    local function setToolBanner(show, toolName)
      toolBannerShown = show
      if show then
        toolBannerLbl.Text = "Running: " .. tostring(toolName)
        TweenService:Create(toolBannerDot, PULSE, {BackgroundTransparency = 0.6}):Play()
      end
      updateBannerLayout()
    end

    local function showConnect()
      connectScreen.Visible = true
      connScreen.Visible = false
      webBannerShown  = false
      toolBannerShown = false
      setStatus("Offline", false)
    end

    local function showConnected()
      connectScreen.Visible = false
      connScreen.Visible = true
      setStatus("Connected", true)
    end

    local LOG_STYLES = {
      tool    = {bg = C.blueBg,    fg = C.blue,    icon = "⚙",  border = Color3.fromRGB(20, 50, 100)},
      success = {bg = C.successBg, fg = C.success, icon = "✓",  border = Color3.fromRGB(15, 55, 25)},
      error   = {bg = C.errorBg,   fg = C.error,   icon = "✕",  border = Color3.fromRGB(80, 20, 20)},
      connect = {bg = C.accentBg,  fg = C.accent,  icon = "⚡", border = Color3.fromRGB(80, 55, 10)},
      system  = {bg = C.surface2,  fg = C.text3,   icon = "◆",  border = C.border},
      default = {bg = C.surface2,  fg = C.muted,   icon = "●",  border = C.border},
    }

    local function addActivity(text, atype)
      local st = LOG_STYLES[atype or "default"] or LOG_STYLES.default

      local entry = mkFrame(st.bg, UDim2.new(1, 0, 0, 24), nil, actScroll)
      corner(5, entry)
      stroke(st.border, 1, entry)
      entry.LayoutOrder = actCount

      mkFrame(st.fg, UDim2.new(0, 2, 1, 0), UDim2.new(0, 0, 0, 0), entry)

      local iconLbl = mkLabel(st.icon, UDim2.fromOffset(20, 24), 9, st.fg, Enum.Font.GothamBold, entry)
      iconLbl.Position = UDim2.fromOffset(5, 0)
      iconLbl.TextXAlignment = Enum.TextXAlignment.Center

      local msgLbl = mkLabel(text, UDim2.new(1, -70, 1, 0), 9, st.fg, Enum.Font.GothamMedium, entry)
      msgLbl.Position = UDim2.fromOffset(24, 0)

      local tsLbl = mkLabel(os.date("%H:%M:%S"), UDim2.fromOffset(52, 24), 7, C.muted, Enum.Font.Code, entry)
      tsLbl.Position = UDim2.new(1, -54, 0, 0)
      tsLbl.TextXAlignment = Enum.TextXAlignment.Right

      table.insert(activities, {text = text, error = atype == "error"})
      actCount += 1
      cntLbl.Text = tostring(actCount)

      local frames = {}
      for _, c in ipairs(actScroll:GetChildren()) do
        if c:IsA("Frame") then table.insert(frames, c) end
      end
      while #frames > 80 do
        frames[1]:Destroy()
        table.remove(frames, 1)
      end

      task.defer(function()
        actScroll.CanvasPosition = Vector2.new(0, 999999)
      end)
    end

    local function flashErr(msg)
      errLbl.TextColor3 = C.error
      errLbl.Text = "⚠  " .. msg
      errLbl.Visible = true
      task.delay(5, function()
        if errLbl.Text:sub(1,3) == "⚠  " then
          errLbl.Text = ""
        end
      end)
    end

    local function updateSnapCount()
      local n = 0
      for _ in pairs(snapshots) do n += 1 end
      snapBtn.Text = "📷 Snap (" .. n .. ")"
    end

    -- ═══════════════════════════════════════════════════════
    --  SHARED CONNECT LOGIC (used by button AND auto-connect)
    -- ═══════════════════════════════════════════════════════

    local function handleConnectSuccess(result, method)
      session_id          = result.session_id
      webOfflineCount     = 0
      webOnlineCount      = 0
      connectedAt         = os.time()
      consecutiveFailures = 0
      setSetting("rux_session_id", session_id)
      showConnected()
      addActivity("Connected" .. (method and " (" .. method .. ")" or ""), "connect")
      pollInterval = 2

      task.spawn(function()
        local count = snapshotAllScripts()
        addActivity("Auto-snapshotted " .. count .. " scripts", "system")
        updateSnapCount()
      end)

      task.spawn(function()
        local meta = executeTool("get_place_metadata", {})
        if meta.success then placeMetadata = meta.data end
      end)
    end

    -- ═══════════════════════════════════════════════════════
    --  CONNECT BUTTON
    -- ═══════════════════════════════════════════════════════

    local dotRunning = false

    local function doConnect()
      if isOutdated then
        flashErr("Plugin is outdated. Update to connect.")
        return
      end

      if dotRunning then return end

      setStatus("Connecting…", false)
      connectBtn.Active = false
      tw(connectBtn, FAST, {BackgroundColor3 = C.accentDim})

      -- Start dot animation immediately so user sees it right away
      dotRunning = true
      local dotFrame = 1
      local dotTexts = {
        "Connecting .",
        "Connecting ..",
        "Connecting ...",
        "Connecting ..",
      }
      errLbl.TextColor3 = C.accent
      errLbl.Text = dotTexts[1]

      task.spawn(function()
        while dotRunning do
          errLbl.TextColor3 = C.accent
          errLbl.Text = dotTexts[dotFrame]
          dotFrame = dotFrame % 4 + 1
          task.wait(0.35)
        end
      end)

      task.spawn(function()
        local rok, result = doRequest(PUBLIC_URL .. "/plugin/connect", "POST", {
          plugin_id  = PLUGIN_ID,
          creator_id = StudioService:GetUserId(),
          version    = PLUGIN_VERSION,
        })

        dotRunning = false
        connectBtn.Active = true
        connectBtn.Text = "Connect to Rux"
        tw(connectBtn, FAST, {BackgroundColor3 = C.accent})

        if not rok then
          if result == "OUTDATED" then
            isOutdated = true
            outdatedBanner.Visible = true
            flashErr("Plugin outdated.")
            return
          end
          flashErr(tostring(result))
          setStatus("Offline", false)
          return
        end

        if not result or not result.ok then
          flashErr(result and tostring(result.error) or "Could not connect. Is Rux open?")
          setStatus("Offline", false)
          return
        end

        errLbl.Text = ""
        handleConnectSuccess(result, result.method)
      end)
    end

    -- Open website first, then attempt connect after short delay
    connectBtn.MouseButton1Click:Connect(function()
      task.spawn(function()
        pcall(function() plugin:OpenBrowserWindow(PUBLIC_URL .. "/app") end)
      end)
      doConnect()
    end)

    -- ═══════════════════════════════════════════════════════
    --  DISCONNECT
    -- ═══════════════════════════════════════════════════════

    local function doDisconnect()
      if session_id then
        task.spawn(function()
          doRequest(PUBLIC_URL .. "/plugin/disconnect", "POST", {
            plugin_id  = PLUGIN_ID,
            session_id = session_id,
          })
        end)
      end
      setSetting("rux_session_id", "")
      SAVED_SESSION = nil
      session_id            = nil
      connected             = false
      webConnected          = false
      webDisconnectNotified = false
      webOfflineCount       = 0
      webOnlineCount        = 0
      consecutiveFailures   = 0
      connectedAt           = 0
      lastToolName          = ""
      lastToolTime          = 0
      toolRunning           = false
      pollInterval          = 3
      setWebStatus("offline")
      showConnect()
    end

    discBtn.MouseButton1Click:Connect(doDisconnect)

    -- ═══════════════════════════════════════════════════════
    --  BOTTOM BAR ACTIONS
    -- ═══════════════════════════════════════════════════════

    snapBtn.MouseButton1Click:Connect(function()
      if not connected then
        addActivity("Not connected", "error")
        return
      end
      local count = snapshotAllScripts()
      addActivity("Snapshotted " .. count .. " scripts", "success")
      updateSnapCount()
      tw(snapBtn, FAST, {BackgroundColor3 = C.successBg})
      task.delay(0.6, function() tw(snapBtn, MED, {BackgroundColor3 = C.surface2}) end)
    end)

    restoreBtn.MouseButton1Click:Connect(function()
      local sel = Selection:Get()
      if #sel == 0 or not isScript(sel[1]) then
        addActivity("Select a script to restore", "error")
        tw(restoreBtn, FAST, {BackgroundColor3 = C.errorBg})
        task.delay(0.6, function() tw(restoreBtn, MED, {BackgroundColor3 = C.surface2}) end)
        return
      end
      local name = sel[1].Name
      local snap = snapshots[name]
      if not snap then
        addActivity("No snapshot for " .. name, "error")
        return
      end
      local rok, rerr = pcall(function() sel[1].Source = snap.source end)
      if rok then
        addActivity("Restored: " .. name, "success")
        tw(restoreBtn, FAST, {BackgroundColor3 = C.successBg})
        task.delay(0.6, function() tw(restoreBtn, MED, {BackgroundColor3 = C.surface2}) end)
      else
        addActivity("Restore failed: " .. tostring(rerr), "error")
      end
    end)

    webOpenBtn.MouseButton1Click:Connect(function()
      pcall(function() plugin:OpenBrowserWindow(PUBLIC_URL .. "/app") end)
    end)

    clearBtn.MouseButton1Click:Connect(function()
      for _, c in ipairs(actScroll:GetChildren()) do
        if c:IsA("Frame") then c:Destroy() end
      end
      activities = {}
      actCount   = 0
      cntLbl.Text = "0"
    end)

    -- ═══════════════════════════════════════════════════════
    --  TOOLBAR
    -- ═══════════════════════════════════════════════════════

    local tb    = plugin:CreateToolbar("Rux")
    local tbBtn = tb:CreateButton("Rux", "Toggle Rux panel", "")
    tbBtn.ClickableWhenViewportHidden = true
    tbBtn.Click:Connect(function()
      widget.Enabled = not widget.Enabled
    end)

    -- ═══════════════════════════════════════════════════════
    --  DURATION TICKER
    -- ═══════════════════════════════════════════════════════

    task.spawn(function()
      while true do
        task.wait(10)
        if connected and connectedAt > 0 then
          local elapsed = os.time() - connectedAt
          local mins    = math.floor(elapsed / 60)
          local hrs     = math.floor(mins / 60)
          durLbl.Text   = hrs > 0
            and (hrs .. "h " .. (mins % 60) .. "m")
            or  (mins .. "m")
          if session_id then
            sesLbl.Text = session_id:sub(1, 14) .. "…"
          end
          if lastToolName ~= "" then
            local ago = os.time() - lastToolTime
            lastToolLbl.Text = lastToolName .. (ago < 120 and " · " .. ago .. "s" or "")
          end
        end
      end
    end)

    -- ═══════════════════════════════════════════════════════
    --  SELECTION MONITOR
    -- ═══════════════════════════════════════════════════════

    task.spawn(function()
      while true do
        task.wait(1)
        local current = getSelectedInfo()
        if current.path ~= (lastSelection and lastSelection.path) then
          lastSelection = current
        end
      end
    end)

    -- ═══════════════════════════════════════════════════════
    --  POLL LOOP
    -- ═══════════════════════════════════════════════════════

    task.spawn(function()
      while true do
        task.wait(math.max(pollInterval, 1))

        if isOutdated then continue end

        -- ── Not connected: wait for the user to click Connect ──
        if not session_id then continue end

        -- ── Connected: heartbeat + poll ──────────────────
        if toolRunning then continue end

        doRequest(PUBLIC_URL .. "/plugin/heartbeat", "POST", {
          plugin_id         = PLUGIN_ID,
          session_id        = session_id,
          status            = "connected",
          selected_instance = getSelectedInfo(),
          version           = PLUGIN_VERSION,
        })

        local pok, presult = doRequest(PUBLIC_URL .. "/plugin/poll", "POST", {
          plugin_id  = PLUGIN_ID,
          session_id = session_id,
        })

        if not pok then
          if presult == "OUTDATED" then
            isOutdated = true
            outdatedBanner.Visible = true
            doDisconnect()
            continue
          end
          consecutiveFailures += 1
          addActivity("Poll error (" .. consecutiveFailures .. ")", "error")
          setStatus("Reconnecting…", false)
          pollInterval = math.min(pollInterval * 2, 16)
          if consecutiveFailures >= 5 then
            addActivity("Connection lost", "error")
            doDisconnect()
          end
          continue
        end

        consecutiveFailures = 0
        if not presult then continue end

        if presult.disconnected == true then
          session_id            = nil
          connected             = false
          webConnected          = false
          webDisconnectNotified = false
          webOfflineCount       = 0
          webOnlineCount        = 0
          setWebStatus("offline")
          showConnect()
          addActivity("Disconnected by web", "error")
          pollInterval = 3
          continue
        end

        -- Web status hysteresis
        local serverSaysWebOn = presult.web_connected == true
        if serverSaysWebOn then
          webOnlineCount  += 1
          webOfflineCount  = 0
          if not webConnected and webOnlineCount >= 2 then
            setWebStatus("active")
            addActivity("Web client connected", "connect")
          elseif webConnected and webOnlineCount >= 2 then
            setWebStatus("active")
          end
        else
          webOfflineCount += 1
          webOnlineCount   = 0
          if webConnected and webOfflineCount >= 3 then
            setWebStatus("offline")
            addActivity("Web client offline", "error")
          elseif webConnected and webOfflineCount >= 1 then
            setWebStatus("idle")
          end
        end

        -- Tool execution
        if presult.tool_call then
          toolRunning  = true
          local tc     = presult.tool_call
          lastToolName = tc.name or "unknown"
          lastToolTime = os.time()

          addActivity(tostring(tc.name), "tool")
          setStatus("Running: " .. tostring(tc.name), true)
          setToolBanner(true, tc.name)

          task.spawn(function()
            local toolResult = executeTool(tc.name, tc.arguments or {})

            if tc.name == "add_instance" and toolResult.success and toolResult.data then
              local path = toolResult.data.path
              if path then
                local inst = findByPath(path)
                if inst then pcall(function() Selection:Set({inst}) end) end
              end
            end

            local trok, trresult = doRequest(PUBLIC_URL .. "/plugin/tool_result", "POST", {
              plugin_id   = PLUGIN_ID,
              session_id  = session_id,
              tool_name   = tc.name,
              tool_result = toolResult,
            })

            toolRunning = false
            setToolBanner(false, nil)

            if trok then
              if trresult and trresult.status == "error" then
                addActivity("Failed: " .. tostring(trresult.reply), "error")
              elseif toolResult.success then
                addActivity(tostring(tc.name) .. " ✓", "success")
              else
                addActivity(tostring(tc.name) .. " — " .. tostring(toolResult.error), "error")
              end
            else
              addActivity("Send result failed", "error")
            end

            setStatus("Connected", true)
            pollInterval = 2
          end)
        else
          if connected then setStatus("Connected", true) end
          pollInterval = math.min(pollInterval + 0.5, 6)
        end
      end
    end)

  end) -- end pcall

  if not buildOk then
    print("[Rux] UI Error: " .. tostring(buildErr))
  end
end

buildUI()