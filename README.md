# Thorlabs CCT10 EPICS IOC

EPICS Channel Access IOC for the Thorlabs CCT10 compact CMOS spectrometer. It
also supports the CCT11/CCT12 devices exposed by the same Thorlabs CCT SDK.

The IOC uses the cross-platform Thorlabs CCT .NET instrument driver through
`pythonnet`. The proprietary SDK is **not** redistributed here. A deterministic
CCT10 simulator is included, so EPICS integration can be developed without the
instrument or vendor libraries.

## Quick start with the simulator

Python 3.10 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[test]'
thorlabscct10-ioc --simulate --prefix CCT10:
```

From a second shell with EPICS command-line tools installed:

```sh
caget CCT10:Connected CCT10:DeviceId
caput CCT10:Exposure 10
caput CCT10:Average 5
caput CCT10:Acquire Acquire
caget CCT10:NumPixels CCT10:PeakWavelength CCT10:PeakIntensity
caget -a CCT10:Spectrum
```

For continuous acquisition:

```sh
caput CCT10:Period 0.5
caput CCT10:Continuous On
```

## Docker deployment

The container includes Python, caproto, pythonnet and the Microsoft .NET 8
runtime. Thorlabs' proprietary SDK is not included and must be mounted into the
container. Use the complete **.NET 8** SDK/output directory, including all DLLs
and any `runtimes` subdirectory; do not use the `net48` build.

Tagged releases are published for `linux/amd64` and `linux/arm64` at:

```text
ghcr.io/infn-epics/thorlabscct
```

For example:

```sh
docker pull ghcr.io/infn-epics/thorlabscct:0.1.0
```

Copy the example configuration and edit the two required paths/addresses:

```sh
cp .env.example .env
docker compose build
docker compose up -d
docker compose logs -f
```

The hardware compose service uses host networking. This is the recommended
deployment on Linux because it allows both CCT Ethernet discovery/traffic and
EPICS Channel Access UDP broadcasts to work normally. Supplying `SERVERIP`
also allows the SDK to connect when automatic discovery is unavailable.

The image can also be run directly:

```sh
docker build -t thorlabscct10-ioc:latest .
docker run --rm --network host \
  -e PREFIX=LAB:CCT10: \
  -e SERVERIP=192.168.0.160 \
  -e EXPOSURE_MS=10 \
  -e AVERAGE=5 \
  -v /opt/vendor/thorlabs-cct/net8.0:/opt/thorlabs/cct:ro \
  thorlabscct10-ioc:latest
```

To validate the image without a device or SDK:

```sh
docker compose -f compose.simulator.yaml up --build
```

### Container startup variables

| Variable | Default | Meaning |
|---|---:|---|
| `PREFIX` | `CCT10:` | Prefix prepended to every EPICS PV |
| `SERVERIP` | empty | CCT Ethernet IPv4 address registered with the SDK |
| `SDK_PATH` | `/opt/thorlabs/cct` | SDK directory inside the container |
| `DEVICE_ID` | empty | Exact SDK connection key; first discovered device otherwise |
| `EXPOSURE_MS` | `8.3` | Initial exposure, 0.01 to 30000 ms |
| `AVERAGE` | `1` | Initial hardware average, 1 to 10000 |
| `SHUTTER` | `open` | Initial shutter state: `open` or `closed` |
| `CONTINUOUS` | `false` | Begin periodic acquisition at startup |
| `PERIOD` | `1.0` | Continuous request period, 0.03 to 3600 seconds |
| `IOC_INTERFACES` | `0.0.0.0` | Comma-separated IOC listen interfaces |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `LOG_PV_NAMES` | `false` | Print the PV inventory at startup |
| `SIMULATE` | `false` | Use the built-in simulator; SDK not required |
| `VIRTUAL` | `false` | Enable the virtual device supplied by the SDK |

Additional IOC command-line arguments may be placed after the image name and
are appended by the entrypoint. Environment variables should be preferred for
the options listed above.

## Hardware setup

1. Install the **CCT Series Spectrometer Software** from Thorlabs. Confirm the
   spectrometer works in ThorSpectra before starting the IOC.
2. Locate the SDK assembly directory. In the Thorlabs Python example this is
   the `pyCCT/net48` directory containing
   `Thorlabs.ManagedDevice.CompactSpectrographDriver.dll` and its dependencies.
   On Linux/macOS use the .NET 8 SDK assemblies supplied by Thorlabs rather
   than the .NET Framework 4.8 build.
3. Install this IOC with its hardware dependency:

   ```sh
   python3 -m pip install -e '.[hardware]'
   ```

4. Start the IOC. USB discovery chooses the first device when `--device-id` is
   omitted:

   ```sh
   thorlabscct10-ioc --prefix LAB:CCT10: --sdk-path /opt/thorlabs/cct/pyCCT/net8
   ```

   For Ethernet, register the instrument address before discovery:

   ```sh
   thorlabscct10-ioc --prefix LAB:CCT10: \
     --sdk-path /opt/thorlabs/cct/pyCCT/net8 --ip 192.168.0.160
   ```

Use `--device-id` when multiple instruments are discovered. The value must be
the exact connection key printed by the SDK. Thorlabs' virtual device can be
enabled with `--virtual`.

## Process variables

| PV suffix | Access | Meaning |
|---|---:|---|
| `Connected` | RO | SDK connection state |
| `DeviceId` | RO | selected SDK connection key |
| `LastError` | RO | most recent driver/connection error |
| `Exposure` | RW | exposure in ms, 0.01 to 30000 |
| `Average` | RW | hardware average, 1 to 10000 frames |
| `Shutter` | RW | `Closed` / `Open` |
| `Acquire` | WO-like | write `Acquire` to queue one spectrum |
| `AcquireDark` | WO-like | close shutter, update dark spectrum, reopen |
| `Continuous` | RW | periodic acquisition enable |
| `Period` | RW | continuous request period in seconds |
| `Busy` | RO | acquisition in progress |
| `Wavelength` | RO | wavelength axis in nm (waveform) |
| `Spectrum` | RO | intensity in counts (waveform) |
| `NumPixels` | RO | valid elements in both waveforms |
| `Sequence` | RO | successful acquisition counter |
| `Timestamp` | RO | Unix time of the last successful spectrum |
| `PeakWavelength` | RO | wavelength at maximum intensity |
| `PeakIntensity` | RO | maximum intensity |

Waveforms allow up to 4096 elements: 2048 for CCT10 and 4096 for CCT11/CCT12.
Acquisition and dark capture are serialized. Driver operations run outside the
Channel Access event loop, so even a 30-second exposure does not freeze the IOC.

## Development

```sh
python3 -m pip install -e '.[test]'
pytest
thorlabscct10-ioc --simulate --list-pvs
```

Hardware tests require a locally installed Thorlabs SDK and are intentionally
not part of the unit-test suite.

## References

- [Thorlabs compact CMOS spectrometers](https://www.thorlabs.com/compact-cmos-spectrometers)
- [Official Thorlabs CCT Python example](https://github.com/Thorlabs/Light_Analysis_Examples/blob/main/Python/Thorlabs%20CCT%20Spectrometers/CCT_example_pythonnet.py)
# thorlabscct
