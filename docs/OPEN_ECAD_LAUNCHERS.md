# Launching from inside the ECAD program (phase 2)

Phase 1 is `polybench --from-open-ecad` (see the README): the command line asks the bridges what is open. Phase 2 would start Polybench from a button inside the ECAD program. Not built. Readiness: nothing here is validated.

## KiCad action plugin

- A Python action plugin (`pcbnew.ActionPlugin`, or the KiCad 10 plugin directory with `plugin.json`) in the user's plugins folder, with a toolbar button on the PCB editor.
- It runs `polybench --from-open-ecad --ecad kicad --protocol <chosen> --data-dir <chosen>` in a child process, and opens the browser page it prints. It must not import Polybench into KiCad's interpreter.
- Needs: the install path of the `polybench` command (KiCad's Python is not the Polybench environment), a folder and protocol picker (a `wx` dialog), the API server switched on, and a decision on where the plugin is installed (Polybench never edits KiCad's configuration; the user copies it, as with `polybench ai setup`).
- Open question: the KiCad 10 plugin API changed; check it against the installed version before writing it.

## Fusion add-in

- A Fusion add-in (Python, `.manifest` plus script) under the user's AddIns folder, with a command in the Electronics toolbar.
- It starts the same command line, with `--ecad fusion`. Fusion reports no file path, so the project folder and protocol are always chosen by the user.
- Needs: Fusion's MCP server on (the bridge already depends on it), an add-in manifest signed or loaded as a script add-in, and a way to find the `polybench` command from Fusion's environment.
- Open question: whether an add-in can start a long-running local process without Fusion stopping it when the add-in is unloaded.

## Both

The launcher may pass `--document NAME` to name the open document or schematic. The command takes an exact name first, then a part of a name that matches exactly one, and otherwise refuses and lists the candidates, so a launcher should pass the full name it knows.


One launcher, one controller: it only starts the command and never talks to the ECAD API itself. No launcher may select, edit or save in the design.
