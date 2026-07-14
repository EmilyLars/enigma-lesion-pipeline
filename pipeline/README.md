# Pipeline scripts

The scripts in this directory are baked into the container image at
`/opt/enigma-lesion/` during the build (see `%files` in `../enigma-lesion.def`).

## Files

| File                              | Purpose                                       |
|-----------------------------------|-----------------------------------------------|
| `pipeline.sh`                     | Main orchestrator; parses flags, dispatches steps |
| `prep_subjects.py`                | Build subject list from demographics CSV/XLSX |
| `run_brain_extraction_worker.sh`  | Per-subject extraction (ants/synthstrip/usermask) |
| `check_dimensions.sh`             | Verify T1 and lesion mask have same dimensions |
| `register_simple.py`              | Lesion-masked SyN registration (default)      |
| `register_with_lesion.py`         | Label-based registration variant              |
| `run_registration_worker.sh`      | Per-subject registration wrapper              |
| `enigma_qc_images.py`             | Pure-Python QC image generator                |
| `enigma_qc_server.py`             | Web-based QC review interface                 |

## Note

**`prep_subjects.py` and `check_dimensions.sh` are still on the cluster
and need to be added to this repo before the first release.** They will
be added in an initial commit soon.
