from fireworks import FiretaskBase, explicit_serialize

from pymatgen.io.vasp.inputs import *
from pymatgen.io.vasp.outputs import Chgcar
from pymatgen.io.vasp.sets import MPStaticSet, MVLGWSet, MPHSEBSSet
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.symmetry.bandstructure import HighSymmKpath

from atomate.common.firetasks.glue_tasks import get_calc_loc, PassResult, \
    CopyFiles, CopyFilesFromCalcLoc
from atomate.vasp.database import VaspCalcDb
from atomate.utils.utils import env_chk

from monty.shutil import compress_dir, decompress_dir

from glob import glob

import shutil, gzip, os, re, traceback, time


@explicit_serialize
class RmSelectiveDynPoscar(FiretaskBase):
    def run_task(self, fw_spec):
        input_strucutre = Structure.from_file("POSCAR")
        if "selective_dynamics" in input_strucutre.site_properties.keys():
            input_strucutre.remove_site_property("selective_dynamics")
            input_strucutre.to("POSCAR", "POSCAR")

@explicit_serialize
class SelectiveDynmaicPoscar(FiretaskBase):

    required_params = ["selective_dynamics", "nsites"]

    def run_task(self, fw_spec):
        where = []
        for i in range(self["nsites"]):
            if i in self["selective_dynamics"]:
                where.append([True, True, True])
            else:
                where.append([False, False, False])
        poscar_selective = Poscar.from_file("POSCAR")
        poscar_selective.selective_dynamics = where
        poscar_selective.write_file("POSCAR")


@explicit_serialize
class JWriteScanVaspStaticFromPrev(FiretaskBase):
    """
    Writes input files for a static run. Assumes that output files from a
    previous (e.g., optimization) run can be accessed in current dir or
    prev_calc_dir. Also allows lepsilon (dielectric constant) calcs.

    Optional params:
        potcar_spec (bool): Instead of writing the POTCAR, write a
            "POTCAR.spec". This is intended to allow testing of workflows
            without requiring pseudo-potentials to be installed on the system.
        (documentation for all other optional params can be found in
        MPStaticSet)

    """

    optional_params = [
        "prev_calc_dir",
        "reciprocal_density",
        "small_gap_multiply",
        "standardize",
        "sym_prec",
        "international_monoclinic",
        "lepsilon",
        "other_params",
        "potcar_spec",
    ]

    def run_task(self, fw_spec):
        lepsilon = self.get("lepsilon")

        # more k-points for dielectric calc.
        default_reciprocal_density = 200 if lepsilon else 100
        other_params = self.get("other_params", {})
        user_incar_settings = other_params.get("user_incar_settings", {})

        # for lepsilon runs, set EDIFF to 1E-5 unless user says otherwise
        if (
                lepsilon
                and "EDIFF" not in user_incar_settings
                and "EDIFF_PER_ATOM" not in user_incar_settings
        ):
            if "user_incar_settings" not in other_params:
                other_params["user_incar_settings"] = {}

        updates = {
            "ADDGRID": True,
            "LASPH": True,
            "LDAU": False,
            "LMIXTAU": True,
            "METAGGA": "SCAN",
            "NELM": 200,
        }
        other_params["user_incar_settings"].update(updates)

        vis = MPStaticSet.from_prev_calc(
            prev_calc_dir=self.get("prev_calc_dir", "."),
            reciprocal_density=self.get(
                "reciprocal_density", default_reciprocal_density
            ),
            small_gap_multiply=self.get("small_gap_multiply", None),
            standardize=self.get("standardize", False),
            sym_prec=self.get("sym_prec", 0.1),
            international_monoclinic=self.get(
                "international_monoclinic", True
            ),
            lepsilon=lepsilon,
            **other_params
        )

        potcar_spec = self.get("potcar_spec", False)
        vis.write_input(".", potcar_spec=potcar_spec)


@explicit_serialize
class JWriteMVLGWFromPrev(FiretaskBase):
    """
    Writes input files for a static run. Assumes that output files from a
    previous (e.g., optimization) run can be accessed in current dir or
    prev_calc_dir. Also allows lepsilon (dielectric constant) calcs.

    Optional params:
        potcar_spec (bool): Instead of writing the POTCAR, write a
            "POTCAR.spec". This is intended to allow testing of workflows
            without requiring pseudo-potentials to be installed on the system.
        (documentation for all other optional params can be found in
        MPStaticSet)

    """

    optional_params = [
        "prev_calc_dir",
        "prev_incar",
        "nbands",
        "reciprocal_density",
        "mode",
        "nbands_factor",
        "ncores",
        "other_params"
    ]

    def run_task(self, fw_spec):

        other_params = self.get("other_params", {})
        user_incar_settings = other_params.get("user_incar_settings", {})

        if "user_incar_settings" not in other_params:
            other_params["user_incar_settings"] = {}

        # updates = {
        #     # "ADDGRID": True,
        #     # "LASPH": True,
        #     # "LDAU": False,
        #     # "LMIXTAU": True,
        #     # "METAGGA": "SCAN",
        #     # "NELM": 200,
        # }
        # other_params["user_incar_settings"].update(updates)
        print(self.get("nbands"), self.get("nbands_factor"), self.get("ncores"))
        vis = MVLGWSet.from_prev_calc(
            prev_calc_dir=self.get("prev_calc_dir", "."),
            prev_incar=self.get("prev_incar", None),
            nbands=self.get("nbands", None),
            reciprocal_density=self.get("reciprocal_density", 100),
            mode=self.get("mode", "DIAG"),
            copy_wavecar=False,
            nbands_factor=self.get("nbands_factor", 5),
            ncores=self.get("ncores", 16),
            **other_params
        )

        vis.write_input(".")

@explicit_serialize
class JFileTransferTask(FiretaskBase):
    """
    A Firetask to Transfer files.

    Before using, cp login/.ssh/id_rsa.pub to local/.ssh/authorized_keys
    then, it must already have successful scp from login to local computer, i.e.
    in OWLS: scp -P 12346 any_file jengyuantsai@localhost:any_path

    Required params:
        - mode: (str) - move, mv, copy, cp, copy2, copytree, copyfile, rtransfer
        - files: (["all"]), ([str]) or ([(str, str)]) - list of source files, or dictionary containing
                'src' and 'dest' keys
        - dest: (str) destination directory, if not specified within files parameter (else optional)

    Optional params:
        - server: (str) server host for remote transfer
        - user: (str) user to authenticate with on remote server
        - key_filename: (str) optional SSH key location for remote transfer
        - max_retry: (int) number of times to retry failed transfers; defaults to `0` (no retries)
        - retry_delay: (int) number of seconds to wait between retries; defaults to `10`
    """
    required_params = ["mode", "files", "dest"]
    optional_params = ["server", "user", "key_filename", "max_retry", "retry_delay"]

    fn_list = {
        "move": shutil.move,
        "mv": shutil.move,
        "copy": shutil.copy,
        "cp": shutil.copy,
        "copy2": shutil.copy2,
        "copytree": shutil.copytree,
        "copyfile": shutil.copyfile,
    }

    def run_task(self, fw_spec):
        shell_interpret = self.get('shell_interpret', True)
        ignore_errors = self.get('ignore_errors')
        max_retry = self.get('max_retry', 0)
        retry_delay = self.get('retry_delay', 10)
        mode = self.get('mode', 'move')
        key_filename = env_chk(self.get('key_filename'), fw_spec)

        if mode == 'rtransfer':
            # remote transfers
            # Create SFTP connection
            import paramiko
            ssh = paramiko.SSHClient()
            # ssh.load_host_keys(os.path.expanduser(os.path.join("~", ".ssh", "known_hosts")))
            ssh.load_system_host_keys()
            ssh.connect(self['server'], username=self.get('user'),
                        key_filename=os.path.expanduser(os.path.join("~", ".ssh", "id_rsa")), port=12346)
            sftp = ssh.open_sftp()

        for f in self["files"]:
            try:
                if "all" == f:
                    src = os.getcwd().split("/")[-1]
                    dest = os.path.join(self["dest"], src)
                    # sftp.mkdir(dest)

                    for file in glob("*"):
                        try:
                            sftp.put(file, os.path.join(dest, file))
                        except FileNotFoundError:
                            sftp.mkdir(dest)
                            sftp.put(file, os.path.join(dest, file))
                else:
                    if 'src' in f:
                        src = os.path.abspath(os.path.expanduser(os.path.expandvars(f['src']))) if shell_interpret else f['src']
                    else:
                        src = abspath(os.path.expanduser(os.path.expandvars(f))) if shell_interpret else f

                    if mode == 'rtransfer':
                        dest = self['dest']
                        if os.path.isdir(src):
                            if not self._rexists(sftp, dest):
                                sftp.mkdir(dest)

                            for f in os.listdir(src):
                                if os.path.isfile(os.path.join(src,f)):
                                    sftp.put(os.path.join(src, f), os.path.join(dest, f))
                        else:
                            if not self._rexists(sftp, dest):
                                sftp.mkdir(dest)

                            sftp.put(src, os.path.join(dest, os.path.basename(src)))

                    else:
                        if 'dest' in f:
                            dest = os.path.abspath(os.path.expanduser(os.pathexpandvars(f['dest']))) if shell_interpret else f['dest']
                        else:
                            dest = os.path.abspath(os.path.expanduser(os.path.expandvars(self['dest']))) if shell_interpret else self['dest']
                        FileTransferTask.fn_list[mode](src, dest)

            except:
                traceback.print_exc()
                if max_retry:

                    # we want to avoid hammering either the local or remote machine
                    time.sleep(retry_delay)
                    self['max_retry'] -= 1
                    self.run_task(fw_spec)

                elif not ignore_errors:
                    raise ValueError(
                        "There was an error performing operation {} from {} "
                        "to {}".format(mode, self["files"], self["dest"]))

        if mode == 'rtransfer':
            sftp.close()
            ssh.close()

    @staticmethod
    def _rexists(sftp, path):
        """
        os.path.exists for paramiko's SCP object
        """
        try:
            sftp.stat(path)
        except IOError as e:
            if e[0] == 2:
                return False
            raise
        else:
            return True

@explicit_serialize
class JWriteInputsFromDB(FiretaskBase):
    """
    A Firetask to write files:
    Required params:
        - files_to_write: ([{filename:(str), contents:(str)}]) List of dicts with filenames
            and contents
    Optional params:
        - dest: (str) Shared path for files
    """
    required_params = ["db_file", "task_id", "write_chgcar"]
    optional_params = ["dest", "modify_incar"]

    def run_task(self, fw_spec):
        pth = self.get("dest", os.getcwd())
        db = VaspCalcDb.from_db_file(self["db_file"])
        e = db.collection.find_one({"task_id": self.get("task_id")})

        poscar = Poscar.from_dict(e["orig_inputs"]["poscar"])
        poscar.write_file(os.path.join(pth, "POSCAR"))

        incar = Incar.from_dict(e["orig_inputs"]["incar"])
        incar.update(self.get("modify_incar", {}))
        incar.write_file(os.path.join(pth, "INCAR"))

        kpoints = Kpoints.from_dict(e["orig_inputs"]["kpoints"])
        kpoints.write_file(os.path.join(pth, "KPOINTS"))

        if self.get("write_chgcar"):
            chgcar = db.get_chgcar(self["task_id"])
            chgcar.write_file(os.path.join(pth, "CHGCAR"))

@explicit_serialize
class WriteTwoDBSKpoints(FiretaskBase):
    optional_params = ["added_kpoints", "reciprocal_density", "kpoints_line_density", "mode"]
    def run_task(self, fw_spec):
                 #structure, added_kpoints=None, reciprocal_density=50, kpoints_line_density=20, mode="line")
        structure = None
        try:
            structure = Structure.from_file("POSCAR")
        except Exception:
            structure = Structure.from_file("POSCAR.gz")

        added_kpoints = self.get("added_kpoints", [])
        reciprocal_density = self.get("reciprocal_density", 50)
        kpoints_line_density = self.get("kpoints_line_density", 20)
        mode = self.get("mode", "line")

        kpts = []
        weights = []
        all_labels = []

        # for both modes, include the Uniform mesh w/standard weights
        grid = Kpoints.automatic_density_by_vol(structure, reciprocal_density).kpts
        ir_kpts = SpacegroupAnalyzer(structure, symprec=0.1).get_ir_reciprocal_mesh(
            grid[0]
        )
        for k in ir_kpts:
            if round(k[0][2], 1) != 0:
                continue
            kpts.append(k[0])
            weights.append(int(k[1]))
            all_labels.append(None)

        # for both modes, include any user-added kpoints w/zero weight
        for k in added_kpoints:
            kpts.append(k)
            weights.append(0.0)
            all_labels.append("user-defined")

        # for line mode only, add the symmetry lines w/zero weight
        if mode.lower() == "line":
            kpath = HighSymmKpath(structure)
            frac_k_points, labels = kpath.get_kpoints(
                line_density=kpoints_line_density, coords_are_cartesian=False
            )

            two_d_kpt, two_d_kpt_label = [], []
            for kpt, klabel in zip(frac_k_points, labels):
                if round(kpt[2], 1) == 0:
                    two_d_kpt.append(kpt)
                    two_d_kpt_label.append(klabel)
            frac_k_points, labels = two_d_kpt, two_d_kpt_label

            for k, f in enumerate(frac_k_points):
                kpts.append(f)
                weights.append(0.0)
                all_labels.append(labels[k])

        comment = (
            "HSE run along symmetry lines"
            if mode.lower() == "line"
            else "HSE run on uniform grid"
        )

        Kpoints(
            comment=comment,
            style=Kpoints.supported_modes.Reciprocal,
            num_kpts=len(kpts),
            kpts=kpts,
            kpts_weights=weights,
            labels=all_labels,
        ).write_file("KPOINTS")


@explicit_serialize
class WriteVaspHSEBSFromPrev(FiretaskBase):
    """
    Writes input files for HSE band structure run. Assumes that output files
    from a previous job can be accessed. Since HSE always re-optimizes the
    charge density (no nSCF mode), the previous job is used to get the location
    of VBM/CBM for mode="gap" (otherwise just used to get the structure /
    starting charge density).

    Optional params:
        potcar_spec (bool): Instead of writing the POTCAR, write a
            "POTCAR.spec". This is intended to allow testing of workflows
            without requiring pseudo-potentials to be installed on the system.
        (documentation for all other optional params can be found in
        MPHSEBSSet)
    """

    required_params = []
    optional_params = [
        "prev_calc_dir",
        "mode",
        "reciprocal_density",
        "kpoints_line_density",
        "potcar_spec",
        "other_params"
    ]

    def run_task(self, fw_spec):
        vis = MPHSEBSSet.from_prev_calc(
            self.get("prev_calc_dir", "."),
            mode=self.get("mode", "uniform"),
            reciprocal_density=self.get("reciprocal_density", 50),
            kpoints_line_density=self.get("kpoints_line_density", 10),
            copy_chgcar=False,
            **self.get("other_params", {})
        )
        potcar_spec = self.get("potcar_spec", False)
        vis.write_input(".", potcar_spec=potcar_spec)

