# ENIGMA Lesion Registration Pipeline

A containerized pipeline for brain extraction, lesion-masked registration, and quality control for the ENIGMA Brain Injury working group.

![CI](https://github.com/EmilyLars/enigma-lesion-pipeline/actions/workflows/lint.yml/badge.svg)

## What this does

Takes T1-weighted MRI images and lesion masks and processes them through:

1. **Prepare subject list** — reads demographics and matches subjects to scans
2. **Brain extraction** — ANTs age-appropriate templates or SynthStrip (lesion-tolerant)
3. **QC: brain extraction** — web-based visual review
4. **Dimension check** — verifies T1 and lesion mask alignment
5. **Registration** — warps to template space with lesion-masked cost function
6. **QC: registration** — web-based visual review of warped outputs

Output: lesion masks in template space, ready for VLSM, TBM, or other group-level analyses. For those downstream analyses, see the companion package [lesionkit](https://github.com/EmilyLars/lesionkit).

## Key features

- **Two brain extraction methods**: ANTs (default) and SynthStrip. SynthStrip is more robust to focal lesions and atypical scanner profiles.
- **Marker-based reprocessing**: `--rerun-failed` reprocesses only subjects without a success marker, letting you rescue ANTs failures with SynthStrip without re-running everything.
- **Support for hand-edited masks**: `--extract-mask DIR` uses pre-supplied brain masks for subjects where automated methods fail.
- **Cluster support**: SLURM, SGE, PBS/Torque schedulers plus local execution.
- **Web-based QC**: no host FSL required (pure Python + matplotlib). Includes lightbox zoom, jump-to-subject, keyboard shortcuts, and pass/fail/review ratings persisted in SQLite.
- **Offline-capable**: all templates and neural network weights pre-cached inside the container.

## Quick start

Requires Apptainer 1.0+ or Singularity 3.6+ on your HPC system. FSL is no longer required.

```bash
# Get the container
wget https://[TODO: Zenodo link]/enigma-lesion.sif

# Get the brain extraction templates (only needed for --extract-method ants)
# See docs/INSTALLATION.md for download links and directory structure

# Organize your data as documented in docs/README.docx, then:
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates \
    enigma-lesion.sif --step all --scheduler slurm /data /templates
```

Full instructions are in [docs/README.docx](docs/README.docx).

## Building the container from source

If you want to rebuild the container yourself (for reproducibility, or to modify it):

```bash
# Clone this repository
git clone https://github.com/EmilyLars/enigma-lesion-pipeline.git
cd enigma-lesion-pipeline

# Build the container (~30-40 minutes, requires internet access)
apptainer build --fakeroot enigma-lesion.sif enigma-lesion.def
```

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for more detail on the build process, prerequisites, and troubleshooting.

## Repository structure

```
enigma-lesion-pipeline/
├── enigma-lesion.def          Apptainer/Singularity build recipe (multi-stage)
├── precache_antsxnet.py       Pre-cache antspynet templates and model weights at build time
├── pipeline/                  Shell + Python scripts baked into the container
│   ├── pipeline.sh              Main orchestrator (--step all, --extract-method, ...)
│   ├── prep_subjects.py         Build subject list from demographics
│   ├── run_brain_extraction_worker.sh   ANTs or SynthStrip per-subject
│   ├── check_dimensions.sh      T1 / lesion mask dimension validation
│   ├── register_simple.py       Lesion-masked SyN registration
│   ├── register_with_lesion.py  Alternative label-based registration
│   ├── run_registration_worker.sh
│   ├── enigma_qc_images.py      Pure-Python QC image generator
│   └── enigma_qc_server.py      Web-based QC review interface
├── docs/                      Documentation (README.docx, install guide, etc.)
├── CHANGELOG.md               Version history
└── README.md                  This file
```

## Container releases

Because the container binary is 2.5 GB, it isn't stored in this repository. Released `.sif` files are hosted on [Zenodo](https://zenodo.org) with a citable DOI per version:

| Version | Date | DOI | Notes |
|---------|------|-----|-------|
| 2.0.0   | TBD  | TBD | Python QC (no host FSL), SynthStrip, offline templates |

## Citation

If you use this pipeline, please cite:

- The ENIGMA Brain Injury working group
- ANTs: Avants et al. (2011), NeuroImage
- SynthStrip (if used): Hoopes et al. (2022), NeuroImage
- ANTsPyNet templates: Tustison et al. (2021), Scientific Reports

## License

MIT — see [LICENSE](LICENSE).

## Related projects

- [lesionkit](https://github.com/EmilyLars/lesionkit) — downstream group-level analyses (overlap maps, VLSM, TBM, harmonization)
- [ENIGMA-U](https://enigma.ini.usc.edu/) — free open-access neuroimaging curriculum
