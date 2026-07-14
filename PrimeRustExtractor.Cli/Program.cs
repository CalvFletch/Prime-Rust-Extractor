using AssetRipper.Export.UnityProjects;
using AssetRipper.Export.UnityProjects.Configuration;
using AssetRipper.Import.Logging;
using AssetRipper.Processing;

if (args.Length == 0)
{
    Console.WriteLine("usage: pre <bundle-or-folder> [more paths...]");
    return 1;
}

Logger.Add(new ConsoleLogger(false));

var settings = new LibraryConfiguration();
var handler = new ExportHandler(settings);

var sw = System.Diagnostics.Stopwatch.StartNew();
GameData gameData = handler.LoadAndProcess(args);
sw.Stop();

var counts = new Dictionary<string, int>();
var total = 0;
foreach (var asset in gameData.GameBundle.FetchAssets())
{
    counts[asset.ClassName] = counts.GetValueOrDefault(asset.ClassName) + 1;
    total++;
}

Console.WriteLine();
Console.WriteLine($"=== PRIME spike: {total} assets in {sw.Elapsed.TotalSeconds:F1}s ===");
foreach (var (className, count) in counts.OrderByDescending(x => x.Value).Take(15))
{
    Console.WriteLine($"{count,8}  {className}");
}
return 0;
