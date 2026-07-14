using System.Text.Json;
using AssetRipper.Import.Structure.Assembly;
using AssetRipper.Import.Structure.Assembly.Serializable;
using AssetRipper.Processing;
using AssetRipper.SourceGenerated.Classes.ClassID_114;

namespace PrimeRustExtractor.Core;

public record CatalogEntry(
    string Kind,          // "item" or "prefab"
    string Name,          // player-facing: "Hot Air Balloon" / cleaned prefab name
    string ShortName,     // "rifle.ak" for items, prefab filename otherwise
    long ItemId,          // 0 for non-items
    string Category,      // item category name, "" for prefabs
    string PrefabGuid,    // "" when unknown
    string PrefabPath);   // "assets/prefabs/..." when known

public class RustCatalog
{
    public string BuildId { get; set; } = "";
    public List<CatalogEntry> Entries { get; set; } = new();

    // Rust's ItemCategory enum, public knowledge from the in-game UI.
    private static readonly string[] CategoryNames =
    [
        "Weapon", "Construction", "Items", "Resources", "Attire", "Tool",
        "Medical", "Food", "Ammunition", "Traps", "Misc", "All", "Common",
        "Component", "Search", "Favourite", "Electrical", "Fun",
    ];

    public static RustCatalog Build(GameData gameData, string buildId)
    {
        var catalog = new RustCatalog { BuildId = buildId };
        var guidToPath = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        // Pass 1: GameManifest → every prefab in the game (guid ↔ path)
        foreach (var monoBehaviour in FetchBehaviours(gameData, "GameManifest"))
        {
            if (monoBehaviour.LoadStructure() is not { } manifest)
            {
                continue;
            }
            if (manifest.TryGetField("prefabProperties") is not { } props)
            {
                continue;
            }
            foreach (var element in props.AsAssetArray)
            {
                if (element is not SerializableStructure prefab)
                {
                    continue;
                }
                var path = (prefab.TryGetField("name")?.AsString ?? "").Replace('\\', '/').ToLowerInvariant();
                var guid = prefab.TryGetField("guid")?.AsString ?? "";
                if (path.Length == 0)
                {
                    continue;
                }
                if (guid.Length > 0)
                {
                    guidToPath[guid] = path;
                }
                catalog.Entries.Add(new CatalogEntry(
                    Kind: "prefab",
                    Name: HumanizePrefabName(path),
                    ShortName: Path.GetFileNameWithoutExtension(path),
                    ItemId: 0,
                    Category: "",
                    PrefabGuid: guid,
                    PrefabPath: path));
            }
        }

        // Pass 2: ItemDefinitions → player vocabulary, joined to prefabs by guid
        foreach (var monoBehaviour in FetchBehaviours(gameData, "ItemDefinition"))
        {
            if (monoBehaviour.LoadStructure() is not { } item)
            {
                continue;
            }
            var shortname = item.TryGetField("shortname")?.AsString ?? "";
            var itemid = item.TryGetField("itemid") is { } id ? unchecked((int)id.PValue) : 0;
            var category = item.TryGetField("category") is { } cat && cat.PValue < (ulong)CategoryNames.Length
                ? CategoryNames[cat.PValue]
                : "";
            var displayName = "";
            if (item.TryGetField("displayName") is { CValue: SerializableStructure phrase })
            {
                displayName = phrase.TryGetField("legacyEnglish")?.AsString
                    ?? phrase.TryGetField("english")?.AsString
                    ?? "";
            }
            var guid = "";
            if (item.TryGetField("worldModelPrefab") is { CValue: SerializableStructure gameObjectRef })
            {
                guid = gameObjectRef.TryGetField("guid")?.AsString ?? "";
            }
            catalog.Entries.Add(new CatalogEntry(
                Kind: "item",
                Name: displayName.Length > 0 ? displayName : shortname,
                ShortName: shortname,
                ItemId: itemid,
                Category: category,
                PrefabGuid: guid,
                PrefabPath: guid.Length > 0 && guidToPath.TryGetValue(guid, out var path) ? path : ""));
        }

        return catalog;
    }

    public IEnumerable<CatalogEntry> Find(string query, string? kind = null, string? category = null, string? pathContains = null)
    {
        var terms = query.ToLowerInvariant().Split(' ', StringSplitOptions.RemoveEmptyEntries);
        foreach (var entry in Entries)
        {
            if (kind != null && !entry.Kind.Equals(kind, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (category != null && !entry.Category.Equals(category, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (pathContains != null && !entry.PrefabPath.Contains(pathContains, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            var haystack = $"{entry.Name} {entry.ShortName} {entry.PrefabPath}".ToLowerInvariant();
            if (terms.All(haystack.Contains))
            {
                yield return entry;
            }
        }
    }

    private static IEnumerable<IMonoBehaviour> FetchBehaviours(GameData gameData, string className)
    {
        foreach (var asset in gameData.GameBundle.FetchAssets())
        {
            if (asset is IMonoBehaviour monoBehaviour && monoBehaviour.ScriptP?.ClassName_R.String == className)
            {
                yield return monoBehaviour;
            }
        }
    }

    private static string HumanizePrefabName(string path)
    {
        var file = Path.GetFileNameWithoutExtension(path);
        return file.Replace('_', ' ').Replace('-', ' ');
    }

    // ---- persistence (build-id keyed, RAE's lesson) ----

    private static string CacheDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PrimeRustExtractor", "catalog");

    public static string CachePath(string buildId) => Path.Combine(CacheDir, $"{buildId}.json");

    public void Save()
    {
        Directory.CreateDirectory(CacheDir);
        File.WriteAllText(CachePath(BuildId), JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = false }));
    }

    public static RustCatalog? Load(string buildId)
    {
        var path = CachePath(buildId);
        if (!File.Exists(path))
        {
            return null;
        }
        return JsonSerializer.Deserialize<RustCatalog>(File.ReadAllText(path));
    }

    public static RustCatalog? LoadNewest()
    {
        if (!Directory.Exists(CacheDir))
        {
            return null;
        }
        var newest = new DirectoryInfo(CacheDir).GetFiles("*.json").OrderByDescending(f => f.LastWriteTimeUtc).FirstOrDefault();
        return newest == null ? null : JsonSerializer.Deserialize<RustCatalog>(File.ReadAllText(newest.FullName));
    }
}
