# Contributing to enigma-lesion-pipeline

Thanks for your interest. This pipeline is community-driven and welcomes contributions.

## Reporting bugs and requesting features

Open an issue on GitHub with:

- The exact command you ran and full terminal output
- Container version (from `apptainer inspect enigma-lesion.sif`)
- Host OS and Apptainer/Singularity version
- Whether the issue is at the container level (build/import) or pipeline level (a specific step)

## Contributing code

1. Fork the repo and clone your fork
2. Create a feature branch
3. Make your changes in `pipeline/` or `enigma-lesion.def`
4. Test locally (build the container, run through a small dataset)
5. Update the `docs/README.docx` if your change affects user-facing behavior
6. Open a pull request

## Testing changes

The container is expensive to build (30-40 minutes), so during development it's useful to work with a "scratch" build:

```bash
# Build once
apptainer build --fakeroot enigma-lesion.sif enigma-lesion.def

# Iterate on pipeline scripts without rebuilding: mount your source dir over
# the container's copy
apptainer exec \
    --bind $(pwd)/pipeline:/opt/enigma-lesion \
    --bind /path/to/testdata:/data \
    --bind /path/to/templates:/templates \
    enigma-lesion.sif \
    /opt/enigma-lesion/pipeline.sh --step ... /data /templates
```

That way you can edit shell/Python scripts and re-run without rebuilding.

## Releasing a new container version

1. Update `CHANGELOG.md`
2. Tag the release: `git tag v2.1.0 && git push origin v2.1.0`
3. Build the container fresh: `apptainer build --fakeroot enigma-lesion.sif enigma-lesion.def`
4. Upload the `.sif` to Zenodo (linked to the previous version so DOIs are versioned)
5. Update the DOI table in `README.md` with the new Zenodo DOI

## Contact

For questions that don't fit an issue, email emily.dennis@utah.edu.
