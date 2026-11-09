# Sources and credits

- **invsqrt** — syed3000's (Syed Asad Ali) version-5 62-qubit file, byte for byte, from https://www.kaggle.com/code/syed3000/vqcc-own-circuit-portfolio-notebook (Apache-2.0), on
  the public invsqrt lineage (mrmeows (team selenium) → busyaprime (Akmal Xodarev) → yoyobar), as credited there.

- **dqi** — syed3000's (Syed Asad Ali) version-7 decoder, byte for byte, from https://www.kaggle.com/code/syed3000/vqcc-own-circuit-portfolio-notebook
  (Apache-2.0), which builds on their own
  decoder line (made with GPT-6 Astra on SomeJoint's kit, as credited there) with ports of SomeJoint's (Agniv Banerjee)
  892-qubit work and of Ali Haydar Özdağ's (ahoirg) idea, and cites Khattar & Gidney (2024). SomeJoint's notebook credits
  GPT-6 Astra Pro, Claude, Ali Haydar Özdağ (ahoirg), yoyobar and the earlier decoder line (Alex Chilton, jday96314
  (QuantumAI), mrmeows, manwithacat, Dmitrii Gluzdov, ViberTeam, Phuc Thai (DePie)).

- **dfthcsos** — syed3000's (Syed Asad Ali) own (8, 143, 44) DF-THC table, its angle words and 15 angle circuits,
  byte for byte, from https://www.kaggle.com/code/syed3000/vqcc-own-circuit-portfolio-notebook (table version 4, angle circuits version 9; Apache-2.0), made there with their
  loader compiler and lattice-aware
  rounding on Lera's (marculera) DF-THC pipeline (https://www.kaggle.com/code/marculera/df-thc-10x-compact-groups-from-the-hamiltonian,
  Apache-2.0) and Ahmed Berat Özer's progressive rounding, as credited there; refitting the weights up to the error gate
  was first shown publicly by heliosli.
  The table weights (W, H2_I) in this version are our refinement of syed3000's refit of that table.
  Lera's sources, as credited there: Ahmed Berat Özer (ahmedberatozer), somejoint, mrmeows, heliosli, lightningv08,
  kozykappa, Kozykorpesh and Sumit Chongder, Artem1410 (anazemcev), haideptry via phucthaiv02, Michael Rustad, k256net
  and Dmitrii Gluzdov, and the hosts.

  Lera's NOTICE (Apache-2.0), as carried in ayodejiibrahimlateef's archive:

  > DF-THC compact groups pipeline
  > Copyright 2026 marculera (Kaggle)
  > 
  > Licensed under the Apache License, Version 2.0 (see LICENSE). The weights, seeds and tables in this dataset were
  > trained here and derive from the competition's FeMoCo Hamiltonian; the arrays H1_I and Hsym_lower in them are the
  > hosts' fixed shifts, as published in the starter kit (sohier/verifiable-circuit-compilation-starter-kit).
  > 
  > Parts of the pipeline in dfthc_refit/ adapt or reimplement public work. Each module's docstring names its source:
  > 
  > - refine.py: adapted from chemistry_refine.py of ahmedberatozer/quantum-ai-df-thc-group-compression (notebook
  >   code, Apache-2.0), which credits kozykappa's sparse column rotations and anazemcev's effective-norm method.
  > - quantize.py, experiments/quantize_rm.py: ahmedberatozer's staged progressive rounding and the fine start of its
  >   schedule (vqcc-v8-components); michaelrustad's L-BFGS stages (hybrid quantization). Own code.
  > - experiments/rmcode.py: somejoint's coded low bit (qcc-our-chemistry-b143-c60-xor2, Apache-2.0). Own code.
  > - tensorqrom.py: mrmeows' exact lookup emitter design (qcc-1375-open-circuits, Apache-2.0), heliosli's scratch bit,
  >   somejoint's codes. Own code.
  > - fourqrom.py: ANF loading and erasure as in the public banked-QROM code (banked_lab.py in ahmedberatozer's
  >   group-compression notebook, after Kozykorpesh and Sumit Chongder). Own construction.
  > - onebody.py: the role of mrmeows' refresh_one_body8.py; own code. polish_words.py: phucthaiv02's exact word-level
  >   coordinate descent after rounding; own version.
  > - refit_weights.py: phucthaiv02's joint (w, W) refit at fixed words (qcc-verified-portfolio-df-thc-retraining-study,
  >   Apache-2.0); our own implementation. experiments/refit_al.py puts it in augmented-Lagrangian form.
  > 
  > No file in this dataset is copied from a dataset under other terms. Tables derived from ahmedberatozer's
  > factorization datasets (license "other") are not included; the notebook records their measured values instead.

- **Organizers** — task definitions, verifiers and kickmix (Google Quantum AI / Qualtran, competition data).
