# Installation Guide

This document covers installing the pipeline container, the
Apptainer/Singularity versions known to work on common operating systems,
how to bind-mount your data, and the path conventions you need to follow
when launching the container.

For workflow-level documentation (running the pipeline, choosing brain
extraction methods, reprocessing failures), see `BRAIN_EXTRACTION_README.md`.

## Quick start

```bash
# 1. Make sure Apptainer (or Singularity) is installed and >= 1.0
apptainer --version

# 2. Pull or build the container
apptainer build enigma-lesion.sif enigma-lesion.def

# 3. Make sure FSL is available on the host
module load fsl   # or however your site loads FSL

# 4. Run with absolute paths (see below)
apptainer run \
    --bind /absolute/path/to/data:/data,/absolute/path/to/templates:/templates \
    enigma-lesion.sif \
    --step all --scheduler slurm /data /templates
```

## Compatible Apptainer/Singularity versions

The pipeline uses multi-stage container builds (added in Apptainer 1.0 /
Singularity 3.2) and standard bind-mount semantics. Here is what beta
testers have verified working:

| OS                     | Apptainer/Singularity version                | Notes                                   |
|------------------------|----------------------------------------------|-----------------------------------------|
| Ubuntu 22.04           | Apptainer 1.4.5                              | Recommended                             |
| RHEL 9 / Rocky 9       | Apptainer 1.4.5-3.el9                        | Confirmed on CHPC and LONI              |
| CentOS 7               | Apptainer 1.1.x or older Singularity         | See note below                          |
| Debian 12              | Apptainer 1.2.x                              | Should work; not directly tested        |
| macOS / Windows native | None                                         | Use Docker Desktop or a Linux VM        |

### CentOS 7 special case

Amsterdam UMC encountered significant difficulty installing current
Apptainer on CentOS 7, which has reached end-of-life and is missing
several modern dependencies (recent glibc, squashfs-tools, fuse3).
What worked for them:

- Avoid trying to install the latest Apptainer release. The 1.4.x line
  requires libraries that CentOS 7 cannot easily provide.
- Install an older Apptainer (1.1.x or 1.2.x) or the equivalent
  Singularity release, both of which support multi-stage builds.
- Several dependencies will need to be downloaded manually from sources
  that may require IT whitelisting on locked-down systems. Plan for this
  to take time and budget for back-and-forth with your sysadmin.

If your site is locked to CentOS 7 and you cannot install any recent
container runtime, an alternative is to **build the container on a more
modern workstation and copy the `.sif` file across**. The `.sif` is a
single file and Apptainer is forward-compatible — a `.sif` built with
Apptainer 1.4 generally runs on Apptainer 1.1.

## Bind mounts and absolute paths

Apptainer does not bind your data directories into the container by
default. You have to tell it explicitly which host paths to expose, and
**all bind-mount paths must be absolute**.

The pipeline expects two directories bound into the container:

- A data directory containing `T1s/`, `LesionMasks/`, and your
  demographics file.
- A templates directory containing `NKI/` and `NKI10AndUnder/`
  (the ANTs brain extraction templates).

A correct command looks like this:

```bash
apptainer run \
    --bind /scratch/edennis/lesion_study:/data \
    --bind /home/edennis/ENIGMA_templates:/templates \
    enigma-lesion.sif \
    --step all --scheduler slurm /data /templates
```

A few common mistakes:

- **Relative paths in `--bind`:** `--bind ./data:/data` will fail or
  silently bind the wrong directory depending on Apptainer version.
  Always use full paths starting with `/`.
- **Spaces in path names:** Apptainer handles them poorly; rename
  directories to avoid spaces or use shell quoting carefully.
- **Symlinks across mount boundaries:** if `/scratch/edennis/lesion_study`
  is itself a symlink to a path that isn't bound into the container, the
  symlink will dangle inside the container. Either bind the symlink
  target as well, or resolve the symlink: `--bind "$(readlink -f /scratch/edennis/lesion_study):/data"`.
- **Forgetting `--bind` entirely:** the container will start, but
  `pipeline.sh` will exit immediately because it cannot find `/data`.

### Paths after bind

Inside the container, you refer to the bound paths by their *in-container*
location, not their host location. In the example above, the data
directory is at `/data` and the templates are at `/templates` regardless
of where they live on the host. The two positional arguments to
`pipeline.sh` (data directory and templates directory) should match the
in-container paths.

### Paths in demographics.csv

The subject IDs in your demographics file should not contain absolute or
relative paths — they are just identifiers (e.g. `sub-001`, `LLU_034_T2`)
that the pipeline uses to build expected filenames like
`<T1_DIR>/<subject>_T1.nii.gz`.

If your demographics file has a path column, drop it before running.

## FSL on the host

The container does not bundle FSL because FSL has its own licensing
constraints. You need to make sure FSL is available on the host system
before running the container. The two QC steps (`qc-extract` and
`qc-register`) call `fslreorient2std`, `overlay`, `slicer`, and
`pngappend`, so those FSL binaries must be on `PATH`.

On most HPC systems:

```bash
module load fsl
apptainer run ...
```

Apptainer inherits `$FSLDIR` and `$PATH` from the host shell. If your
site does not use environment modules, bind-mount your FSL install
directly:

```bash
apptainer run --bind $FSLDIR:/usr/local/fsl ...
```

## Network requirements

**Runtime: no outbound internet is required.** All templates and model
weights are pre-cached inside the container during build. Sites with
restricted VMs (the Amsterdam UMC case) can run the full pipeline
without whitelisting any external domains.

**Build time** does need outbound access to the following hosts:

- `docker.io` (ubuntu base image, freesurfer/synthstrip image)
- `github.com` (ANTs release tarball)
- `pypi.org` (Python packages)
- `download.pytorch.org` (CPU PyTorch wheel)
- `ndownloader.figshare.com` (ANTsXNet template precache)

If you are building behind a restrictive firewall, ask your sysadmin to
whitelist these. If even that isn't possible, build the `.sif` on a
permissive machine and copy it across.

## Verifying your install

After building, a quick sanity check before running on real data:

```bash
# 1. The container runs and prints help
apptainer run enigma-lesion.sif --help

# 2. SynthStrip binary is present and callable
apptainer exec enigma-lesion.sif which mri_synthstrip
apptainer exec enigma-lesion.sif mri_synthstrip --help 2>&1 | head -5

# 3. ANTs is available
apptainer exec enigma-lesion.sif which antsBrainExtraction.sh

# 4. ANTsPyNet cache is populated
apptainer exec enigma-lesion.sif ls /opt/antsxnet-cache/

# 5. Python imports work
apptainer exec enigma-lesion.sif python3 -c "import ants, antspynet; print('ok')"
```

If any of these fail, the container is not built correctly and running
the full pipeline will produce confusing errors. Rebuild before
proceeding.

## Building offline / on restricted networks

Some sites (particularly EU clinical environments and government VMs)
cannot reach external hosts even during container build. The Amsterdam UMC
team hit this in beta testing — their VM required whitelisting Figshare,
Docker Hub, and an AWS S3 bucket before the build would complete.

The cleanest answer if your *runtime* environment is restricted but your
*build* environment is not: **build on a permissive machine and copy the
`.sif`**. A `.sif` file is a single self-contained file. Copy it via SCP,
USB drive, or whatever your IT policy allows, and it will run wherever
Apptainer is installed.

If you cannot find any permissive machine, you'll need to pre-download
every external resource and rebuild from a local mirror. The build
fetches from these hosts:

| Host                       | What it provides                       |
|----------------------------|----------------------------------------|
| `docker.io`                | `ubuntu:22.04` base, `freesurfer/synthstrip:1.6` |
| `github.com`               | ANTs release tarball                   |
| `pypi.org`                 | Python packages                        |
| `download.pytorch.org`     | CPU PyTorch wheel                      |
| `ndownloader.figshare.com` | ANTsXNet templates and model weights   |

There are two reasonable strategies for getting these onto a restricted
build host.

### Strategy A: Whitelist the build-time hosts

The simplest fix if your sysadmin is willing — open temporary outbound
access to those five domains for the duration of the build, then close
them again. The build takes 15-30 minutes. None of these hosts are
contacted at runtime, so this is a one-time cost.

### Strategy B: Local mirror of all dependencies

For air-gapped environments where Strategy A isn't possible, you can
pre-stage everything on the host filesystem and tweak the `.def` to read
from there. This requires editing `enigma-lesion.def`. Outline:

1. **On a permissive machine**, run these to collect the artifacts:

   ```bash
   mkdir -p offline_deps
   cd offline_deps

   # ANTs binary release
   wget https://github.com/ANTsX/ANTs/releases/download/v2.5.3/ants-2.5.3-ubuntu-22.04-X64-gcc.zip

   # Pull base images as OCI tarballs
   apptainer pull docker_ubuntu.sif docker://ubuntu:22.04
   apptainer pull docker_synthstrip.sif docker://freesurfer/synthstrip:1.6

   # Python wheels (collect everything into one directory)
   pip3 download \
       --index-url https://download.pytorch.org/whl/cpu \
       torch==2.3.1 -d wheels/
   pip3 download \
       surfa antspyx antspynet nibabel pandas openpyxl -d wheels/

   # ANTsXNet template and model files: see precache_antsxnet.py
   # for the list of names. Easiest is to run that script on a permissive
   # machine, then tar up the resulting /opt/antsxnet-cache directory.
   ```

2. **Transfer `offline_deps/` to the restricted host** (scp, rsync, USB).

3. **Modify `enigma-lesion.def`** to read from local paths instead of
   external URLs. Replace `wget` calls with `cp` from your offline_deps
   directory, point the `pip install` commands at `--find-links wheels/`
   and add `--no-index`, and copy the precached antsxnet cache directly
   into `/opt/antsxnet-cache/` via the `%files` section.

If you go this route, talk to me first — there are a few subtleties around
the Apptainer multi-stage source-image case (the `freesurfer/synthstrip`
pull) that benefit from a worked example. The current pipeline
maintainers should be able to share a "restricted-build" branch of the
`.def` file on request, or you can open an issue describing your
constraints.

### Strategy C: Pre-built `.sif` distribution

If neither A nor B is workable for your site, the simplest path forward
is to request a pre-built `.sif` from the pipeline coordinators. Ship it
to your restricted host on physical media if necessary. Verify with
`sha256sum` against a hash provided out-of-band.

## Troubleshooting

**"FATAL: container creation failed: mount /data: no such file or directory"**
You forgot `--bind`, or used a host path that doesn't exist. Use absolute
paths and verify the host directory exists.

**"Permission denied" when writing to the data directory**
The container runs as your user by default, so you need write access
to the host path you bound. If your data directory is owned by a group
account, you may need `chmod g+w` or to run with `--userns` enabled.

**SLURM jobs submit but immediately fail with "container not found"**
The `.sif` path needs to be accessible from the compute nodes, not just
the login node. Put the `.sif` on shared storage (e.g. `/scratch/...`
or your home directory).

**QC server starts but the browser shows nothing**
Make sure you SSH-tunneled the port: `ssh -L 8890:localhost:8890
user@host`. Then open `http://localhost:8890` in a *local* browser, not
on the server. The QC server only listens on localhost by design (no
external network exposure).

**SQLite "database is locked" error when launching QC**
This was the Amsterdam issue. As of the May 2026 release, the QC
database lives at `~/.cache/enigma-lesion-qc/qc_<hash>.db` on local
disk by default, regardless of where the QC images live. If you are
still seeing the error, you may be on an older container version or
have explicitly set `--db-path` to a network-mounted location.
