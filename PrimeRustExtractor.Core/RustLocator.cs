using System.Text.RegularExpressions;

namespace PrimeRustExtractor.Core;

public record RustInstall(string GameRoot, string BundlesPath, string? BuildId);

/// <summary>
/// Finds Rust installs across Steam libraries. Ported from our Rust Asset Studio fork (MIT, same author).
/// </summary>
public static class RustLocator
{
    private const string RustAppId = "252490";

    public static List<RustInstall> GetInstalls()
    {
        var installs = new List<RustInstall>();
        foreach (var library in GetSteamLibraries())
        {
            var steamApps = Path.Combine(library, "steamapps");
            var installDir = "Rust";
            var appManifest = Path.Combine(steamApps, $"appmanifest_{RustAppId}.acf");
            string? buildId = null;
            if (File.Exists(appManifest))
            {
                var text = File.ReadAllText(appManifest);
                var dirMatch = Regex.Match(text, "\"installdir\"\\s+\"([^\"]+)\"");
                if (dirMatch.Success)
                {
                    installDir = dirMatch.Groups[1].Value;
                }
                var buildMatch = Regex.Match(text, "\"buildid\"\\s+\"([^\"]+)\"");
                if (buildMatch.Success)
                {
                    buildId = buildMatch.Groups[1].Value;
                }
            }

            var root = Path.Combine(steamApps, "common", installDir);
            var bundles = Path.Combine(root, "Bundles");
            if (Directory.Exists(bundles) && File.Exists(Path.Combine(bundles, "Bundles"))
                && !installs.Any(x => string.Equals(x.GameRoot, root, StringComparison.OrdinalIgnoreCase)))
            {
                installs.Add(new RustInstall(root, bundles, buildId));
            }
        }
        return installs;
    }

    private static IEnumerable<string> GetSteamLibraries()
    {
        var steamPath = GetSteamPath();
        if (steamPath == null)
        {
            yield break;
        }
        yield return steamPath;

        var vdf = Path.Combine(steamPath, "steamapps", "libraryfolders.vdf");
        if (!File.Exists(vdf))
        {
            yield break;
        }
        foreach (Match match in Regex.Matches(File.ReadAllText(vdf), "\"path\"\\s+\"([^\"]+)\""))
        {
            var library = match.Groups[1].Value.Replace(@"\\", @"\");
            if (!string.Equals(library, steamPath, StringComparison.OrdinalIgnoreCase) && Directory.Exists(library))
            {
                yield return library;
            }
        }
    }

    private static string? GetSteamPath()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        var path = Microsoft.Win32.Registry.GetValue(@"HKEY_CURRENT_USER\Software\Valve\Steam", "SteamPath", null) as string
            ?? Microsoft.Win32.Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath", null) as string;
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }
        path = path.Replace('/', '\\');
        return Directory.Exists(path) ? path : null;
    }
}
