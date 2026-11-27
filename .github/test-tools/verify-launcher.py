import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


parser = argparse.ArgumentParser(description='Test play.ps1 with a synthetic executable.')
parser.add_argument('launcher', type=Path)
parser.add_argument('--powershell', default=shutil.which('pwsh') or shutil.which('powershell'))
args = parser.parse_args()
if not args.powershell:
    parser.error('Specify the path to PowerShell with --powershell.')
PLAY = args.launcher.resolve(strict=True)
POWERSHELL = args.powershell

PROBE = '''
$ErrorActionPreference = 'Stop'
$before = (Get-Location).Path
$nativeErrors = Test-Path Variable:PSNativeCommandUseErrorActionPreference
if ($nativeErrors) { $PSNativeCommandUseErrorActionPreference = $env:PROBE_EXIT_CODE -ne '0' }
$supportsEmpty = $PSVersionTable.PSVersion -ge [version]'7.5'
if ($supportsEmpty) { Set-Item -LiteralPath Env:VICTORY_LOG -Value '' }
$caught = $false
$setupFailure = $env:PROBE_SETUP_FAILURE -eq '1'
$more = @{ VICTORY_CACHE_DIR = 'run-cache'; PLAYER_ENV_SENTINEL = 'run-only' }
if ($setupFailure) { $more['INVALID=NAME'] = 'bad override' }
try {
    & ../scripts/play.ps1 -Exe '../synthetic title.exe' -More $more
} catch {
    if ($setupFailure -and $_.InvocationInfo.MyCommand.Name -eq 'Set-Item') {
        $caught = $true
    } elseif ($nativeErrors -and $_.FullyQualifiedErrorId -eq 'ProgramExitedWithNonZeroCode') {
        $caught = $true
    } else {
        throw
    }
}
[ordered]@{
    locationRestored = ((Get-Location).Path -ceq $before)
    failureCaught = $caught
    nativeErrors = $nativeErrors
    supportsEmpty = $supportsEmpty
    childExitCode = $LASTEXITCODE
    ambient = $env:VICTORY_AMBIENT
    cache = $env:VICTORY_CACHE_DIR
    gameDir = $env:VICTORY_GAME_DIR
    thin = $env:VICTORY_THIN
    window = $env:VICTORY_WINDOW_MAX
    unrelated = $env:PLAYER_ENV_SENTINEL
    empty = $env:VICTORY_LOG
} | ConvertTo-Json -Compress
'''

ENV_NAMES = (
    'VICTORY_AMBIENT', 'VICTORY_CACHE_DIR', 'VICTORY_GAME_DIR', 'VICTORY_THIN',
    'VICTORY_WINDOW_MAX', 'PLAYER_ENV_SENTINEL', 'VICTORY_LOG',
)

UNIX_CHILD = r'''#!/usr/bin/python3
import os
from pathlib import Path
import sys

keys = {names!r}
values = [os.environ.get(key, '<unset>') for key in keys]
Path('child.txt').write_text('\n'.join([os.getcwd(), *sys.argv[1:], *values]) + '\n')
sys.exit(int(os.environ['PROBE_EXIT_CODE']))
'''

WINDOWS_CHILD = '''
using System;
using System.IO;

public static class Child {
    public static int Main(string[] args) {
        string[] names = { NAMES };
        using (StreamWriter report = File.CreateText("child.txt")) {
            report.WriteLine(Directory.GetCurrentDirectory());
            foreach (string arg in args) {
                report.WriteLine(arg);
            }
            foreach (string name in names) {
                report.WriteLine(Environment.GetEnvironmentVariable(name) ?? "<unset>");
            }
        }
        return int.Parse(Environment.GetEnvironmentVariable("PROBE_EXIT_CODE"));
    }
}
'''


def create_child(executable):
    if os.name != 'nt':
        executable.write_text(UNIX_CHILD.format(names=ENV_NAMES))
        executable.chmod(0o755)
        return

    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET'
    compilers = [framework / name / 'v4.0.30319/csc.exe'
                 for name in ('Framework64', 'Framework')]
    compiler = next((candidate for candidate in compilers if candidate.is_file()), None)
    if compiler is None:
        sys.exit('The .NET Framework 4 C# compiler is required for the Windows fixture.')
    source = executable.with_suffix('.cs')
    source.write_text(WINDOWS_CHILD.replace('NAMES', ', '.join(map(json.dumps, ENV_NAMES))))
    subprocess.run([compiler, '/nologo', f'/out:{executable}', source], check=True)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / 'scripts').mkdir()
    (root / 'target/diag').mkdir(parents=True)
    caller = root / 'calling directory'
    caller.mkdir()
    shutil.copyfile(PLAY, root / 'scripts/play.ps1')
    executable = root / 'target/diag/victory.exe'
    create_child(executable)
    title = root / 'synthetic title.exe'
    title.touch()

    env = {name: value for name, value in os.environ.items() if not name.upper().startswith('VICTORY_')}
    env.update(
        POWERSHELL_TELEMETRY_OPTOUT='1',
        VICTORY_AMBIENT='0.3',
        VICTORY_CACHE_DIR='shell-cache',
        VICTORY_GAME_DIR='shell-game',
        VICTORY_WINDOW_MAX='1280x720',
        PLAYER_ENV_SENTINEL='shell-only',
    )

    failures = []
    child_report = root / 'child.txt'
    for exit_code, setup_failure in ((0, False), (23, False), (0, True)):
        child_report.unlink(missing_ok=True)
        command = [POWERSHELL, '-NoProfile', '-NonInteractive']
        if os.name == 'nt':
            command.extend(('-ExecutionPolicy', 'Bypass'))
        result = subprocess.run(
            [*command, '-Command', PROBE],
            cwd=caller,
            env={**env, 'PROBE_EXIT_CODE': str(exit_code), 'PROBE_SETUP_FAILURE': str(int(setup_failure))},
            text=True,
            capture_output=True,
        )
        if result.returncode:
            sys.exit(result.stdout + result.stderr)
        if setup_failure:
            label = 'Invalid override'
            assert not child_report.exists(), 'launcher executed despite setup failure'
        else:
            label = f'Child exit {exit_code}'
            if not child_report.is_file():
                sys.exit(f'{label}: no child report.\n{result.stdout}{result.stderr}')
            child = child_report.read_text(encoding='utf-8-sig').splitlines()
            assert Path(child[0]).resolve() == root.resolve(), child
            assert child[1] == 'run' and Path(child[2]).resolve() == title.resolve(), child
            assert child[3:] == ['--execute', '0.8', 'run-cache', 'shell-game',
                                 '1', '<unset>', 'run-only', '<unset>'], child

        shell = json.loads(result.stdout.strip().splitlines()[-1])
        expected = {
            'locationRestored': True,
            'failureCaught': setup_failure or (exit_code != 0 and shell['nativeErrors']),
            'childExitCode': None if setup_failure else exit_code,
            'ambient': '0.3',
            'cache': 'shell-cache',
            'gameDir': 'shell-game',
            'thin': None,
            'window': '1280x720',
            'unrelated': 'shell-only',
            'empty': '' if shell['supportsEmpty'] else None,
        }
        for key, value in expected.items():
            if shell[key] != value:
                failures.append(f'{label}: {key} was {shell[key]!r}, expected {value!r}')
        print(f'{label}: child launch behavior verified')

    if failures:
        sys.exit('\n'.join(failures))
    print('The caller environment and working directory were restored in all three cases.')
