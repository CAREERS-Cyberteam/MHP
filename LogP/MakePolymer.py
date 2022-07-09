from functools import cache
from PIL import Image
from rdkit import Chem
from rdkit.Chem import AllChem, Draw, Descriptors, rdFreeSASA
import argparse, os, csv, json
import matplotlib.pyplot as plt
from smiles import monomer_dict, init_dict

def getJsonArgs(jsonFile, dict):
    with open(jsonFile, 'r') as J: #open json file
        runs_dict = json.load(J) #read it
        for run in runs_dict["runs"]: #so few items the nested for loops shouldn't be a big deal
            run_keys = run.keys()
            for dict_key in dict.keys(): #the keys submitted to the func (derrived from CLI arguments)
                if dict_key not in run_keys: #if there is a key provided by user not in the dict derrived from the json file
                    run[dict_key] = dict[dict_key] #then add it
    run_list = runs_dict["runs"] #now we have a list of runs with all arguments from file and command line.
    return run_list

def getArgs():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type = int, help = "The number of monomer or super-monomer units.")
    parser.add_argument("-i", "--initiator", type = str, default = "Hydrogen", help = "Initiator Key from initiator dict or SMILES. Defaults to Hydrogen.")
    parser.add_argument("-t", "--terminator", type = str, default = "Hydrogen", help = "Terminator key taken from initiator dict or SMILES. Defaults to Hydrogen.")
    parser.add_argument("-m","--single_monomer", type = str, help = "Monomer key from the included monomer dict. See the -s flag for specifying a monomer that is not included.")
    parser.add_argument("-s", "--super_monomer", type = str, nargs = '*',
                        help = "A series of space-separated monomer SMILES arranged in their repeating sequence. You can add an int preceeding any monomer to represent multiple copies of that monomer. e.g. 2 A B means AAB is the repeating super-monomer. Use quotes surrounding SMILES with problematic characters like = or ()")
    parser.add_argument("-d", "--draw", type = str, help = "Filename for polymer image.")
    parser.add_argument("-v", "--verbose", default = False, action = "store_true", help = "Set increased verbosity. Will draw polymer to polymer.png unless alternate name set by -d option.")
    parser.add_argument("-c","--calculation", type = str, nargs = '*', 
                        help = "Type of calculation(s) to be performed input as a space-separated list. Options are LogP, SA (surface area), MV (Molecular Volume), MHP (Mathers Hydrophobicity Parameter (LogP/SA; each of which will also be reported. Use XMHP to exclude those plots)) and RG (radius of gyration).")
    parser.add_argument("-f","--file", type = str, help = "The name/path of the file you wish to save the mol to. Supported formats are .pdb, .xyz and .mol")
    parser.add_argument("-r", "--read", type = str, help = "The name/path to file you wish to import. Supported formats are .pdb and .mol")
    parser.add_argument("-p", "--plot", default = False, action = "store_true", 
                        help = "Include this option to generate a plot of whatever calculations are specified with -c on polymers from 1 to the n specified with the -n flag. This means the molecule cannot be read from a file with the -r flag. If used with the -f flag multiple files will be saved with names based off the one provided.")
    parser.add_argument("-e", "--export", type = str, help = "Include this option to export a .csv file of all data calculations. Specify the name here.")
    parser.add_argument("-j", "--json", type = str, help = "The path to a compatible .json file with any of the above arguments.")
    parser.add_argument("-q", "--quiet", default = False, action = "store_true", help = "Add this option to suppress the confirmation step which by default prevents calculations from running until the structure of the polymer is approved.")
    args = parser.parse_args()
    #get additional arguments from json file if provided or by default if no args provided.
    vardict = vars(args)
    if args.json is not None:
        run_list = getJsonArgs(args.json, vardict)
    else:
        run_list = [vardict] #args assumed to be in list later due to above.
    return run_list

def getRepeatUnit(single, super):
    #Two cases: one monomer or supermonomer.
    #If both are specified something is wrong.
    if single is not None and super is not None:
        raise TypeError("Cannot specify both single and super monomers")
    #This gives a list of components of a super-monomer or just the string used for single monomer in dict
    repeat_unit = list(filter(None, [single, super]))[0]
    return repeat_unit

@cache #avoid multiple lookups if multiple runs with same inputs
def monomer_smi_lookup(m):
    repeat_unit = monomer_dict[m]
    return repeat_unit

@cache #avoid multiple lookups if multiple runs with same inputs
def inator_smi_lookup(i,t):
    given_inators = [i,t]
    #gets from dict if available. Otherwise assume SMILES and continue. There will eventually be an error if this isn't the case.
    smiles_inators = [init_dict[x] if x in init_dict else x for x in given_inators]
    init = smiles_inators [0]
    term = smiles_inators[1]
    return init, term

def validate_end_group(inator, *, Init=False, Term=False, verbosity=False):
    if not Init and not Term:
        raise ValueError("Need to specify wether end group is terminator or initiator.")
    
    if Init:
        idx = 0 #look at first char of initiator
    else:
        idx = -1 #look at last character of terminator

    if inator != "" and inator[idx] == "*": #the attatchment point does not face the rest of polymer
        if verbosity:
            print("initiator smiles in wrong direction. Converting to mol object.")
        inator = Chem.MolFromSmiles(inator)
    elif inator != inator[::-1] and "*" not in inator:
        raise ValueError("end group smiles is not palendromic and has no attatchment point specified.")
    else:
        inator = inator.replace("*", "") #remove asterisk if not using rdkit method

    return inator

def get_building_blocks(i,t,m,*, verbosity = False):
    init, term = inator_smi_lookup(i, t)
    
    if type(m) == list:
        #replace any dict keys with corresponding smiles.
        deciphered_dict_keys = [monomer_dict[x] if x in monomer_dict else x for x in m]
        
        #we need an accurate count for number of monomers since a grouping specified by -s can be AABBB.
        #In this example, 1 unit of n is really 5 monomers. We want proper notation in figures.
        explicit_coefs = [int(x) if len(x) == 1 else 0 for x in deciphered_dict_keys] #all monomers are (hopefully) 2 atoms or more. Assume others are coefs.
        sum_implicit_coefs = len(deciphered_dict_keys) - len(explicit_coefs) #number of implicit coefs of 1.
        monomers_per_n = sum(explicit_coefs) + sum_implicit_coefs #total monomer unit count per super-monomer.

        #start with empty repeat unit and concatonate stuff we find in the list.
        repeat_unit = ""
        #ommission of a coeficient implies 1 copy
        repeat_coef = 1
        for element in deciphered_dict_keys:
            try:
                repeat_coef = int(element) #is this a string of an integer?
            except:
                repeat_unit += repeat_coef * element #if not, repeat the SMILES as many times as specified (or once if no coef. provided).
                repeat_coef = 1 #reset coef.
    else:
        repeat_unit = monomer_smi_lookup(m) #if not a list, look for the corresponding smiles in the dictionary, will throw error if not included.
        monomers_per_n = 1

    init = validate_end_group(init, Init=True, verbosity=verbosity)
    term = validate_end_group(term, Term=True, verbosity=verbosity)
    
    return init, term, repeat_unit, monomers_per_n

def attatch_frags(polymer_smiles, *, add_initiator = (False, None), add_terminator = (False, None)): #the initiator and terminator are the kwargs
    pol = Chem.MolFromSmiles(polymer_smiles)
    #get indicies of "*" atoms
    fake_atoms = [a.GetIdx() for a in pol.GetAtoms() if a.GetAtomicNum() == 0]
    #and their neighbors (to which we will actually be attatching.)
    conn_atoms = [pol.GetAtomWithIdx(x).GetNeighbors()[0].GetIdx() for x in fake_atoms]

    #label the head and tail, accounting for possible absense of one or both inators.
    inators = []
    if add_initiator[0]:
        head = pol.GetAtomWithIdx(conn_atoms[0])
        head.SetProp("atomNote", "head")
        inators.append(add_initiator[1])
        if add_terminator[0]:
            tail = pol.GetAtomWithIdx(conn_atoms[1])
            tail.SetProp("atomNote", "tail")
            inators.append(add_terminator[1])
    elif add_terminator[0]:
        tail = pol.GetAtomWithIdx(conn_atoms[0])
        tail.SetProp("atomNote", "tail")
        inators.append(add_terminator[1])
    else:
        raise Exception(f"Unknown combination of inators {add_initiator = }, {add_terminator = }.")

    #set name to what will be used after one loop completes.
    mergedrw = pol
    for inator in inators:
        #see above.
        fake_atoms = [a.GetIdx() for a in inator.GetAtoms() if a.GetAtomicNum() == 0]
        #this time we just isolate atom object instead of index.
        attatch = [inator.GetAtomWithIdx(x).GetNeighbors()[0] for x in fake_atoms][0]
        #label.
        attatch.SetProp("atomNote", "attatch")
        #put the two mols into the same object (still no bond between them.)
        merged = Chem.CombineMols(inator, mergedrw)
        #change to rwmol object which can be changed.
        mergedrw = Chem.RWMol(merged)

        #indicies of atoms with notes
        attachments = [a.GetIdx() for a in mergedrw.GetAtoms() if a.HasProp('atomNote')]
        #isolating proper index from list to use in bond formation.
        inator_attatchment = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "attatch"][0]

        if inator == add_initiator[1]:
            bond_here = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "head"][0]

        if inator == add_terminator[1]:
            bond_here = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "tail"][0]
            
        #make bond
        mergedrw.AddBond(bond_here, inator_attatchment, Chem.rdchem.BondType.SINGLE)
        #change label so that atom is not targeted a second time for bond formation.
        mergedrw.GetAtomWithIdx(inator_attatchment).SetProp('atomNote', 'done')

    #count up number of dummy atoms ("*")
    dummies = [a for a in mergedrw.GetAtoms() if a.GetAtomicNum() == 0]
    numDummies = len(dummies)

    #remove the dummy atoms (need to do one at a time)
    for i in range(numDummies):
        mergedrw.RemoveAtom([a.GetIdx() for a in mergedrw.GetAtoms() if a.GetAtomicNum() == 0][0])

    smi = Chem.MolToSmiles(mergedrw)
    return smi

def add_inator_smiles(smi, init, term, *, verbosity=False):
    if verbosity:
        print(f"polymer smiles is {smi} before any end groups")

    if type(init) != str: #i.e. a mol object instead
        smi = "*" + smi
        add_initiator = True # we will attatch with mol-based methods
    else:
        add_initiator = False
        smi = init + smi #just attatch as string instead
        if verbosity and init != "":
            print(f"polymer smiles is {smi} after adding initiator smiles")
            
    if type(term) != str: #i.e. a mol object instead
        smi = smi + "*" #same as above but for terminator. Attachment point is at end this time.
        add_terminator = True
    else:
        add_terminator = False
        smi = smi + term #same as above but for terminator
        if verbosity and init != "":
            print(f"polymer smiles is {smi} after adding terminator smiles")

    if add_terminator or add_initiator:
        if verbosity:
            print(f"converting polymer body {smi} to mol object to add frags")
        smi = attatch_frags(smi, add_initiator=(add_initiator, init), add_terminator=(add_terminator, term))

    return smi

def createPolymerSMILES(i,n,r,t,*, verbosity = False, test = False):
    init, term, repeat_unit, m_per_n = get_building_blocks(i,t,r, verbosity=verbosity)

    polymer_SMILES = n * repeat_unit
    
    if test: # a parameter used to generate an n=1 image where it is easy to see where end groups attatch
        #if you don't do this and have n=15, the image is very hard to parse visually and some parts of pol will overlap.
        test_smi = repeat_unit
        test_smi = add_inator_smiles(test_smi, init, term, verbosity=verbosity)
        verbosity = False #turn off verbosity for the next generation because we already talk it through the first time.
    
    full_smiles = add_inator_smiles(polymer_SMILES, init, term, verbosity=verbosity)

    if test:
        return test_smi, full_smiles, m_per_n
        #return test smiles too so it can be previewed. It is fast to make both before confirmation
        #but we do the confirmation before optimizing geometry.
    else:
        return full_smiles, m_per_n
   
def optPol(smiles):
    tmp_mol = "tmp.mol"
    #make Mol object:
    pol = Chem.MolFromSmiles(smiles)
    #check mol
    Chem.SanitizeMol(pol)
    #opt steps
    pol_h = Chem.AddHs(pol)
    ids = AllChem.EmbedMultipleConfs(pol_h, numConfs=10, #randomSeed=randomSeed, 
        useExpTorsionAnglePrefs=True, numThreads=0)
    best = []
    for id in ids:
        prop = AllChem.MMFFGetMoleculeProperties(pol_h)
        ff = AllChem.MMFFGetMoleculeForceField(pol_h, prop, confId=id)
        ff.Minimize()
        en = float(ff.CalcEnergy())
        econf = (en, id)
        best.append(econf)
    best.sort()
    best_id = int(best[0][1])
    Chem.MolToMolFile(pol_h,tmp_mol,confId=int(best_id))
    pol_h = Chem.MolFromMolFile(tmp_mol)
    os.remove(tmp_mol)

    # AllChem.EmbedMolecule(pol_h, useRandomCoords=True)
    # AllChem.MMFFOptimizeMolecule(pol_h, maxIters=5000)

    return pol_h, pol

def confirmStructure(smi, *, proceed=None):
    #save image to temporary file
    drawPol(Chem.MolFromSmiles(smi), "confirm.png")
    img = Image.open("confirm.png")
    #show it to user
    img.show()
    inp = input("Does this look right? [Y/n]")
    
    if os.path.exists("confirm.png"):
        os.remove("confirm.png")
        #delete the file

    #affirmation is y, Y or just hitting enter
    if inp.lower() == "y" or inp == "":
        inp = True
        print("Great! If you wish to bypass this confirmation step, use the -q flag when running this script.")
    else:
        inp = False
        print("Please try adjusting input and try again.")
        quit()
        #aborts so user can retry

    if proceed is not None:
        return inp #used to stop plotting jobs from asking for confirmation for each pol those jobs generate.

def make_One_or_More_Polymers(i, n, r, t, *, verbosity=False, plot=False, confirm=False):
    POL_LIST = []
    SMI_LIST = []
    Unopt_pols = []
    if plot:
        N_array = range(1, n+1)

        #this allows us to confirm only once for plotting jobs
        if confirm == True:
            proceed = False
        else:
            proceed = True

        for j in N_array:
            if j == 1 and confirm and not proceed:
                test_smi, smi, m_per_n = createPolymerSMILES(i,j,r,t,verbosity=verbosity, test=True)
                verbosity = False
                proceed = confirmStructure(test_smi, proceed=proceed)
            
            if j > 1 or not confirm: #do not test if j is large or if we ask not to test at all.
                smi, m_per_n = createPolymerSMILES(i, j, r, t, verbosity=verbosity)
            
            if verbosity:
                print(f"Done generating SMILES with n = {j} now: {smi}")
                print("Converting to RDkit mol now.")

            pol_h, pol = optPol(smi)
            POL_LIST.append(pol_h)
            SMI_LIST.append(smi)
            Unopt_pols.append(pol)
        return POL_LIST, SMI_LIST, Unopt_pols, m_per_n
    else:
        test_smi, full_smi, m_per_n = createPolymerSMILES(i, n, r, t, verbosity=verbosity, test=True)
        if verbosity:
            print(f'Polymer interpreted as: {i} {n} * {r} {t}')
            print(f"This gives the following SMILES: {full_smi}")

        if confirm:
            print("Showing structure with n=1 to confirm correct end groups")
            confirmStructure(test_smi)
        
        pol_h, pol = optPol(full_smi) #both are mol objects
        return pol_h, full_smi, pol, m_per_n

def drawPol(pol, drawName, *, mpn=1):
    if type(pol) == list: #save a grid image instead
        img = Chem.Draw.MolsToGridImage(pol, legends = [f"n = {(i + 1) * mpn}" for i, mol in enumerate(pol)], subImgSize=(250, 250))
        #mpn is the number of monomers per "n". This is > 1 when -s is used and multiple monomers or copies of the same monomer are specified.
        img.save(drawName)
    else:
        Chem.Draw.MolToFile(pol, drawName)

def write_or_read_pol(name, *, verbosity=False, read=False, mol=None):
    ext = name.split(".")[1]
    if read:
        if os.path.exists(name):
            #is the file type valid?
            if ext == "pdb":
                pol_h = Chem.MolFromPDBFile(name)
            elif ext == "mol":
                pol_h = Chem.MolFromMolFile(name)
            else:
                print(f"unsuported extention: {ext} Please use .pdb, or .mol") #.xyz cannot be read by rdkit.
                quit()

            polSMILES = Chem.MolToSmiles(pol_h)
            pol = Chem.MolFromSmiles(polSMILES)
            return pol_h, polSMILES, pol
        else:
            raise FileNotFoundError(name)
    else:
        if verbosity:
            print(f'attempting to save molecule to {name}')
        #is the file type valid?
        if ext == "xyz":
            Chem.MolToXYZFile(mol, name)
        elif ext == "pdb":
            Chem.MolToPDBFile(mol, name)
        elif ext == "mol":
            Chem.MolToMolFile(mol, name)
        else:
            print(f"Unsuported extention: {ext} Please use .pdb, .xyz or .mol")
            quit()

        if verbosity:
            print(f'Success')

def Sasa(pol_h):
    # Calculate SASA
    radii = Chem.rdFreeSASA.classifyAtoms(pol_h)
    sasa = Chem.rdFreeSASA.CalcSASA(pol_h, radii)    
    return sasa

def LogP(pol_h):
    # LogP does NOT have an option to feed in a conformer so just calculate it for the overall molecule
    logP = Chem.Descriptors.MolLogP(pol_h)
    return logP

def RadGyration(pol_h):
    RG = Chem.rdMolDescriptors.CalcRadiusOfGyration(pol_h)
    #Chem.Descriptors3D.RadiusOfGyration(pol_h)
    #both seem to give identical results based on "SMILES to Rg.ipynb"
    return RG

def MolVolume(pol_h):
    MV = Chem.AllChem.ComputeMolVolume(pol_h, confId = -1, gridSpacing = 0.2, boxMargin = 2.0)
    return MV

def doCalcs(pol_h, calcs):
    #The variable /calcs/ is a set
    #Calcs are only done if requested.
    #remove entries from set after each calculation and print the unrecognised ones at the end.
    data = {}
    if "SA" in calcs or "MHP" in calcs or "XMHP" in calcs:
        sasa = Sasa(pol_h)
        if not "XMHP" in calcs: #if XMHP is included user eXcluisively wants MHP, so we don't return this data.
            data["SA"] = sasa
        calcs.discard("SA")
    if "LogP" in calcs or "MHP" in calcs or "XMHP" in calcs:
        logP = LogP(pol_h)
        if not "XMHP" in calcs: #if XMHP is included user eXcluisively wants MHP, so we don't return this data.
            data["LogP"] = logP
        calcs.discard("LogP")
    if "RG" in calcs:
        rg = RadGyration(pol_h)
        data["RG"] = rg
        calcs.discard("RG")
    if "MV" in calcs:
        mv  =  MolVolume(pol_h)
        data["MV"] = mv
        calcs.discard("MV")
    if "MHP" in calcs or "XMHP" in calcs:
        mhp = logP / sasa
        data["MHP"] = mhp
        calcs.discard("MHP")
        calcs.discard("XMHP")
    if len(calcs) > 0:
        print(f"Unrecognized calculation(s): {calcs}. Use SA, LogP, MV, MHP, XMHP or RG")
    return data

def makePlot(pol_list, calculations, smiles_list, *, verbosity=False, mpn=1):
    dicts = []
    for i, pol in enumerate(pol_list):
        calcs = set(calculations)
        pol_data = doCalcs(pol, calcs)
        pol_data["N"] = (i + 1) * mpn
        pol_data["smi"] = smiles_list[i]
        dicts.append(pol_data)
    data = {k: [d[k] for d in dicts] for k in dicts[0]}
    
    ncols = len(data) - 2

    if ncols == 1: #matplotlib got angry at me for trying to make a plot with only one subplot. Use plt.plot to avoid this.
        calc_key = [k if k != "XMHP" else "MHP" for k in calculations][0] #use given calc as key unless XMHP, then use MHP.
        plt.plot(data["N"], data[calc_key], 'o')
        plt.title(f'{calc_key} vs n')
        plt.xlabel('n') 
        plt.ylabel(f'{calc_key}')
    else:
        #need to make multiple subplots if multiple calcs requested.
        figure, axis = plt.subplots(ncols = ncols)
        series = 0
        for key in data:
            #we can't plot N vs N or anything to do with smiles
            if key != "N" and key != "smi":
                axis[series].scatter(data["N"], data[key])
                axis[series].set_title(f"{key} vs n")
                series += 1
    figname = "Size-dependent-stats.png"
    plt.savefig(figname, bbox_inches = 'tight')
    print(f'Saved plot to {figname}')
    if verbosity:
        print(data)
        plt.show()
    return data, dicts

def exportToCSV(exptName, data, dicts_list, verbosity=False):
    with open(exptName, "w", newline = "") as c:
        #set column names as dict keys
        cols = list(data.keys())
        writer = csv.DictWriter(c, fieldnames = cols)
        writer.writeheader()
        #write the data.
        writer.writerows(dicts_list)
    print(f"Done exporting data to {exptName}.")
    if verbosity: #this is turned off by main() if plotting is also turned on since both functions can print data and that is only needed once.
        print(data)

def main():
    run_list = getArgs()

    for vardict in run_list:
        if vardict["read"] is None: #then get polymer parameters from CLI arguments.
            repeat_unit = getRepeatUnit(vardict["single_monomer"], vardict["super_monomer"])
                        
            if vardict["plot"]:
                POL_LIST, SMI_LIST, UNOPT_POL_LIST, M_PER_N = make_One_or_More_Polymers(vardict["initiator"], vardict["n"],
                                                        repeat_unit, vardict["terminator"], verbosity=vardict["verbose"], plot=vardict["plot"], confirm = not vardict["quiet"])
            else:
                pol_h, polSMILES, pol, M_PER_N = make_One_or_More_Polymers(vardict["initiator"], vardict["n"],
                                                        repeat_unit, vardict["terminator"], verbosity=vardict["verbose"], plot=vardict["plot"], confirm = not vardict["quiet"])
        else: #get mol from file
            if vardict["plot"]:
                raise TypeError("You may not plot data read from a file.") #we should be able to check for other files with name convention "{name}_{n}.{ext}"
            pol_h, polSMILES, pol = write_or_read_pol(vardict["read"], read=True)
            #pol_h is the as-is (probably 3-D) structure of the molecule. pol is the 2D structure.

        #saving the polymer to a file.
        if vardict["file"] is not None: #technically nothing wrong with using this as a roundabout way of converting between filetypes                
            if vardict["plot"]:
                base = vardict["file"].split(".")[0]
                ext = vardict["file"].split(".")[1]
                for i, mol in enumerate(POL_LIST):
                    name = f"{base}_{i + 1}.{ext}"
                    write_or_read_pol(name, mol=mol)
            else:
                write_or_read_pol(vardict["file"], mol=pol_h)

        #drawing a picture of the polymer.
        if vardict["plot"]:
            pol = UNOPT_POL_LIST #submit this list of mols for use in grid image.
        if vardict["draw"] is not None:
            drawName = f'{vardict["draw"].split(".")[0]}.png'
            drawPol(pol, drawName, mpn=M_PER_N)
        else:
            if vardict["verbose"]:
                #produce image if increased verbosity is requested even if no name is set.
                print("Saving image to polymer.png by default.")
                print(f"{M_PER_N = }")
                drawPol(pol, "polymer.png", mpn=M_PER_N)

        #CALCULATIONS
        if vardict["verbose"]:
            print(f'requested calculations are {vardict["calculation"]}')
        if vardict["calculation"] is not None:
            if not vardict["plot"]:
                calcs = set(vardict["calculation"])
                data = doCalcs(pol_h, calcs) #use set to remove duplicates
                data["N"] = vardict["n"] * M_PER_N
                data["smi"] = polSMILES
                dicts = [data]
                print(data)
            else:
                data, dicts = makePlot(POL_LIST, vardict["calculation"], SMI_LIST, verbosity=vardict["verbose"], mpn=M_PER_N)
                
            if vardict["export"] is not None:
                if vardict["plot"]: #we don't need to print data twice if both -p and -e use verbosity=True
                    verbo = False
                else:
                    verbo = vardict["verbose"]        
                exportToCSV(vardict["export"], data, dicts, verbosity=verbo)

        print("\n") #separating runs visually if more than one.

if __name__ == "__main__":
    main()
