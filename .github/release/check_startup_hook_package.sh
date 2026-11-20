#!/usr/bin/env bash
# Install the exact packed TraceAI.AutoInstrumentation package into clean stock .NET 8 and .NET 10
# applications and require a real AI span to reach a local collector through the installed hook.
#
# Usage: check_startup_hook_package.sh FEED_DIR [VERSION]
#   FEED_DIR  folder holding the packed TraceAI.AutoInstrumentation nupkg (dotnet pack --output)
#   VERSION   package version; read from the nupkg file name when left out
# Needs the .NET 8 and .NET 10 SDK/runtimes, unzip and network access to restore the targeting packs.
# Runs in CI only. Credentials are synthetic and no collector outside this machine is used.
set -euo pipefail

FEED=$(cd "${1:?feed folder with the packed nupkg}" && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SMOKE_APP="$HERE/../../csharp/tests/TraceAI.AutoInstrumentation.PackageSmoke/Program.cs"
VERSION="${2:-}"
if [ -z "$VERSION" ]; then
  for file in "$FEED"/TraceAI.AutoInstrumentation.*.nupkg; do
    name=$(basename "$file" .nupkg)
    rest=${name#TraceAI.AutoInstrumentation.}
    case "$rest" in [0-9]*) VERSION=$rest ;; esac
  done
fi
[ -n "$VERSION" ] || { echo "::error::no TraceAI.AutoInstrumentation nupkg found in $FEED"; exit 1; }
PKG="$FEED/TraceAI.AutoInstrumentation.$VERSION.nupkg"
[ -f "$PKG" ] || { echo "::error::missing $PKG"; exit 1; }

WORK=$(mktemp -d)
export NUGET_PACKAGES="$WORK/nuget-cache"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1

# 1. The packed file: the whole published hook directory, nothing to compile against, no dependencies.
unzip -Z1 "$PKG" > "$WORK/entries.txt"
for entry in \
  tools/net8.0/any/TraceAI.AutoInstrumentation.dll \
  tools/net8.0/any/TraceAI.AutoInstrumentation.Runtime.dll \
  tools/net8.0/any/TraceAI.AutoInstrumentation.HostingStartup.dll \
  tools/net8.0/any/FIInstrumentation.dll \
  tools/net8.0/any/OpenTelemetry.dll \
  tools/net8.0/any/OpenTelemetry.Api.dll \
  tools/net8.0/any/OpenTelemetry.Exporter.OpenTelemetryProtocol.dll; do
  grep -qx "$entry" "$WORK/entries.txt" || { echo "::error::missing $entry in the package"; exit 1; }
done
grep -qx README.md "$WORK/entries.txt" || { echo "::error::missing README.md in the package"; exit 1; }
if grep -q '^lib/' "$WORK/entries.txt"; then echo "::error::the package carries compile assets"; exit 1; fi
if unzip -p "$PKG" '*.nuspec' | grep -q '<dependency '; then echo "::error::the package declares a dependency"; exit 1; fi
echo "PACKAGE_LAYOUT_PASS version=$VERSION"

# 2. The same package, installed from the local feed into a clean app of each runtime and started with the hook.
for tfm in net8.0 net10.0; do
  APP="$WORK/consumer-$tfm"
  mkdir -p "$APP"
  cat > "$APP/consumer-$tfm.csproj" <<XML
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>$tfm</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
XML
  cp "$SMOKE_APP" "$APP/Program.cs"
  dotnet add "$APP" package TraceAI.AutoInstrumentation --version "$VERSION" --source "$FEED" --no-restore
  dotnet restore "$APP" --source "$FEED" --source https://api.nuget.org/v3/index.json
  dotnet build "$APP" --no-restore -c Release
  DLL="$APP/bin/Release/$tfm/consumer-$tfm.dll"
  HOOK="$NUGET_PACKAGES/traceai.autoinstrumentation/$VERSION/tools/net8.0/any/TraceAI.AutoInstrumentation.dll"
  [ -f "$HOOK" ] || { echo "::error::restore did not unpack the hook at $HOOK"; exit 1; }

  # Negative control: without the hook the same app must report that nothing listens (exit 2).
  rc=0
  env -u DOTNET_STARTUP_HOOKS dotnet "$DLL" > "$WORK/no-hook-$tfm.log" 2>&1 || rc=$?
  cat "$WORK/no-hook-$tfm.log"
  printf 'NEGATIVE_CONTROL %s rc=%s expected=2\n' "$tfm" "$rc"
  [ "$rc" -eq 2 ] || { echo "::error::$tfm app without the hook did not report HOOK_NOT_LISTENING"; exit 1; }

  # Positive run. The hook catches every start error, prints a warning and lets the app exit 0,
  # so a warning-and-clean-exit is a failure: the app itself must see its AI span exported.
  rc=0
  env FI_API_KEY=sk-test-PLACEHOLDER FI_SECRET_KEY=sk-test-PLACEHOLDER FI_BASE_URL=http://127.0.0.1:18762 \
    FI_PROJECT_NAME=package-proof FI_AUTO_DEBUG=true FI_CONFIG_FILE="$WORK/absent.toml" \
    DOTNET_STARTUP_HOOKS="$HOOK" dotnet "$DLL" > "$WORK/hook-$tfm.log" 2>&1 || rc=$?
  cat "$WORK/hook-$tfm.log"
  if grep -q 'Agent could not load' "$WORK/hook-$tfm.log"; then echo "::error::the hook could not load in the $tfm app"; exit 1; fi
  [ "$rc" -eq 0 ] || { echo "::error::$tfm app with the hook exited $rc"; exit 1; }
  grep -q INSTALLED_HOOK_EXPORT_PASS "$WORK/hook-$tfm.log" || { echo "::error::$tfm app never saw its AI span exported"; exit 1; }
done
echo "STARTUP_HOOK_PACKAGE_PASS version=$VERSION"
