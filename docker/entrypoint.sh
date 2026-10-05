#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-CCT10:}"
SERVERIP="${SERVERIP:-}"
SDK_PATH="${SDK_PATH:-/opt/thorlabs/cct}"
DEVICE_ID="${DEVICE_ID:-}"
SIMULATE="${SIMULATE:-false}"
VIRTUAL="${VIRTUAL:-false}"
EXPOSURE_MS="${EXPOSURE_MS:-8.3}"
AVERAGE="${AVERAGE:-1}"
SHUTTER="${SHUTTER:-open}"
CONTINUOUS="${CONTINUOUS:-false}"
PERIOD="${PERIOD:-1.0}"
IOC_INTERFACES="${IOC_INTERFACES:-0.0.0.0}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
LOG_PV_NAMES="${LOG_PV_NAMES:-false}"

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

args=(
    --prefix "$PREFIX"
    --exposure "$EXPOSURE_MS"
    --average "$AVERAGE"
    --shutter "$SHUTTER"
    --period "$PERIOD"
    --interfaces "$IOC_INTERFACES"
    --log-level "$LOG_LEVEL"
)

if is_true "$SIMULATE"; then
    args+=(--simulate)
else
    driver="$SDK_PATH/Thorlabs.ManagedDevice.CompactSpectrographDriver.dll"
    if [[ ! -f "$driver" ]]; then
        echo "ERROR: Thorlabs CCT SDK not found at $driver" >&2
        echo "Mount the complete .NET 8 SDK directory at $SDK_PATH or set SIMULATE=true." >&2
        exit 2
    fi
    args+=(--sdk-path "$SDK_PATH")
fi

[[ -n "$SERVERIP" ]] && args+=(--ip "$SERVERIP")
[[ -n "$DEVICE_ID" ]] && args+=(--device-id "$DEVICE_ID")
is_true "$VIRTUAL" && args+=(--virtual)
is_true "$CONTINUOUS" && args+=(--continuous)
is_true "$LOG_PV_NAMES" && args+=(--log-pv-names)

exec thorlabscct10-ioc "${args[@]}" "$@"
