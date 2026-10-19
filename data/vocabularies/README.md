# Vocabularies

This directory holds two lookup tables that let the server answer which species a code stands for and which species make up a functional group. Users meet them through the chat, for example when they ask which species PIL is.

`species.csv` is a copy of the SURIMI western Mediterranean market inputs (`references/SURIMI-western_med/SURIMI-market_inputs/species.csv` in the project monorepo). The server is deployed without that monorepo, so the file is kept here, and the server reads this copy first.

The file has 90 rows and 86 distinct codes. ANE, HKE, MUT and PIL each appear twice, once per life stage, with the same genus and species in both rows, so each of these codes gives one scientific name. The `explain_species_code` tool (`surimi_data_mcp/knowledge/lookups.py`) returns every matching row.

`ewe_functional_groups.csv` lists the 59 Ecopath functional groups of the EwE model in 111 rows, and gives the member species for the fished groups. The `data_list_functional_groups` tool reads it, and the server keeps the source file's spelling, including its typos.

When the market inputs change, copy `species.csv` again from the monorepo.
