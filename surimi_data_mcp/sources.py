"""The data sources the server declares: what each holds, which tools read it and what it cannot answer.

Importing this module registers them in `capabilities`, and the drift check at start-up compares them with the registered tools.
"""
from surimi_data_mcp import capabilities
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.datasets import metrics as metrics_module
from surimi_data_mcp.knowledge import models


def served_count() -> int:
    """Return the number of datasets in the catalogue."""
    return len(catalog.DATASETS)


def served_in(domain: str) -> int:
    """Return the number of catalogue datasets whose `domain` is `domain`."""
    return sum(1 for d in catalog.DATASETS if d.get("domain") == domain)


# ── Capability registry ──

# What this server cannot answer and where the answer lives. `lives_in` is a path to the file that holds it.
capabilities.register(capabilities.DataSourceCapability(
    source_id="eu-dcf",
    title="EU Data Collection Framework: fisheries economics, employment, capacity, fuel",
    domain="fisheries",
    description="Multiple EU DCF tables covering economic performance, employment, fleet capacity, and fuel efficiency across Mediterranean and EU coastal states.",
    coverage={"temporal": "varies by table; list_datasets gives each one's years", "tables": served_in("eu_dcf")},
    tools=("list_datasets", "list_columns", "query_data", "describe_table"),
    cannot_answer=(
        capabilities.CannotAnswer(
            question_class=(
                "how catches WILL change under climate change, or any other "
                "forward projection of catches"
            ),
            why="not_collected",
            detail=(
                "EU_DCF_Catches.csv (6,087,268 rows, quarter/gear/species, "
                "registered as eu-dcf-catches) and the ICES/DCF landings "
                "tables hold OBSERVED catch only, 2013-2024 and earlier for "
                "the ICES series. None of them carries a climate-linked "
                "projection or a scenario mechanism. A forward number would "
                "have to come from a model this server does not run or "
                "read. Do not extrapolate the observed trend and call it a "
                "climate projection."
            ),
            lives_in="references/SURIMI-DATA-LAKE/1_Fisheries/1.3_EU DCF Catches/EU_DCF_Catches.csv",
        ),
        capabilities.CannotAnswer(
            question_class=(
                "what revenue, profit or effort WOULD be under a closure, a "
                "fuel price change, a quota or any other scenario"
            ),
            why="not_served",
            detail=(
                "THIS SERVER cannot run or read a scenario: it serves tabular "
                "observations and holds no path to a simulation. SURIMI the "
                "PROJECT does have the machinery: a scenario-driven coupled "
                "stack (Ecopath, POSEIDON, market, and fisheries-authority, "
                "which sets catch limits per species) behind a SubmitExperiment "
                "RPC in controller_service.proto, and runs exist: "
                "output_creator wrote a 74 MB result on 2026-09-15. Two limits "
                "are real. Those runs land on the model server's S3, not here. "
                "And the run that exists is a HINDCAST ending 2017-01, so a "
                "question about a future scenario has no output behind it even "
                "there. The observed half is often answerable here: the 2022 "
                "fuel shock is in the AER tables this server already serves. "
                "Do not tell a user SURIMI cannot model a counterfactual; say "
                "this server cannot reach the model that does."
            ),
            lives_in="references/SURIMI-protocol/surimi/v1/controller_service.proto",
        ),
    ),
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="oecd-fse",
    title="OECD Fisheries Support Estimates",
    domain="policy",
    description="Government support to fisheries: direct transfers, cost-reducing transfers, and general services. Long-format (per-country, per-year) and wide-format (FSE category breakdown).",
    coverage={"temporal": "2000–2022", "tables": served_in("oecd")},
    tools=("query_data", "describe_table"),
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="surimi-model",
    title="SURIMI simulation outputs",
    domain="modelling",
    description=(
        "Empirical biomass surveys (western Mediterranean, MEDITS CPUE) and a simulated sales "
        "table whose generating run is not documented."
    ),
    coverage={"temporal": "varies", "tables": served_in("surimi")},
    tools=("query_data", "describe_table"),
    cannot_answer=(
        capabilities.CannotAnswer(
            question_class=(
                "what the catches, landings or production WILL BE in coming "
                "years, or any other forward-looking catch or yield question"
            ),
            why="not_collected",
            detail=(
                "DO NOT ANSWER THIS FROM surimi-sales-aegean-simulated. That "
                "table is served, its results carry a calculation record, and it looks like an answer: "
                "144 rows spanning 2025-2040, which is exactly the horizon such "
                "a question asks about. It is SIMULATED SALES, not catch. Its "
                "measure columns are quantity_kt, price_eur_per_kg and "
                "value_meur, which are sold quantity, not gross catch, not landings, "
                "not production. Quantity sold is not catch: it excludes "
                "discards entirely and is a market outcome, not a fishing one. "
                "Real catch data DOES exist and this server does not hold it: "
                "gross_catch_total is written by a SURIMI run and reachable "
                "through the MODEL server, with values from 2013 to 2017-01. "
                "That is a HINDCAST, because its time axis ends 2017-01-01, so it "
                "answers what catches WERE, never what they will be. No forward "
                "catch projection has been run. Point a coming-years question "
                "at neither table. "
                "explain_provenance on surimi-sales-aegean-simulated returns "
                "zero sources, so nothing documents where those rows came from "
                "either. A calculation record shows that a number was computed from "
                "the rows in a table, and it does not show whether the table "
                "answers the question asked. That is why this substitution "
                "is dangerous and not merely wrong."
            ),
            lives_in="references/SURIMI-experiment-output-contract.md",
        ),
        capabilities.CannotAnswer(
            question_class=(
                "what a dataset was USED FOR within the project, which model "
                "consumed it, or what purpose it served"
            ),
            why="not_collected",
            detail=(
                "Call list_datasets and report its documented_use field. Of the "
                "data lake's READMEs only the CMSY++ input files (9_Model Inputs, "
                "Northern Spain GSA06) record a use; no other dataset's use is "
                "written down. The wiki and the gRPC contracts describe how the "
                "models connect to each other, not which dataset fed which run, "
                "and answering from them produces a confident account of "
                "something nobody wrote down. The use of the other datasets "
                "cannot be derived: it has to be written by someone who knows."
            ),
            lives_in=(
                "references/SURIMI-DATA-LAKE/9_Model Inputs/CMSY++/"
                "GSA06_CMSY++_input files/README.txt"
            ),
        ),
        capabilities.CannotAnswer(
            question_class=(
                "what a GEAR code stands for, such as OTB, LLS, PS or ART"
            ),
            why="not_collected",
            detail=(
                "Species codes are answered by explain_species_code, which "
                "reads species.csv (90 rows, 86 distinct codes; ANE, HKE, MUT "
                "and PIL appear twice, split by life stage, so every match is "
                "returned rather than the first). Gear codes are NOT resolved. "
                "western_med_contract.yaml enumerates which gear codes exist "
                "but defines none of them, and ART is undefined in every repo "
                "checked, so no project file supports an answer. The standard "
                "referent is the FAO ISSCFG gear classification, which is not "
                "vendored here."
            ),
            lives_in="references/SURIMI-western_med/SURIMI-contracts/western_med_contract.yaml",
        ),
        capabilities.CannotAnswer(
            question_class=(
                "species abundance, biodiversity or invasive status"
            ),
            why="not_served",
            detail=(
                "Which species make up a functional group IS answerable: call "
                "data_list_functional_groups, which returns the matched group "
                "and its species with scientific name, FAO code and whether "
                "each is fished. Do not refuse that question. "
                "Abundance is the part this server cannot reach. The served "
                "biomass table is 60 rows covering five species in one sea. "
                "MEDITS holds 950,530 rows of survey CPUE across 1,416 species "
                "from 1994 to 2021, and Traits.csv from FishBase and "
                "SeaLifeBase adds 1,229 species. Both ARE registered in "
                "the dataset catalogue (datasets.yaml), as surimi-biomass-medits and "
                "fishbase-sealifebase-traits. Whether they can be queried "
                "depends on the deployment: list_datasets reports "
                "cube_servable false when this deployment's cube does not hold "
                "them, and on such a deployment the backend has no relation "
                "behind them either. Report what list_datasets says, never "
                "that they are unregistered."
            ),
            lives_in=(
                "references/SURIMI-DATA-LAKE/2_Abundance/"
                "2.1_MEDITS CPUE ESTIMATED FAO 37/medits_cpue_all_species_wide.csv"
            ),
        ),
        capabilities.CannotAnswer(
            question_class=(
                "catch, discards, price or sales BY SPECIES AND FLEET, and "
                "anything about the western Mediterranean case study from 2013 "
                "onward"
            ),
            why="not_served",
            detail=(
                "A 53 MB NetCDF sample is kept in the SURIMI development tree, "
                "outside this server and the data lake, and neither server "
                "serves it. It holds 49 "
                "monthly steps from 2013-01 to 2017-01 over 94 species, 9 "
                "fleets, 38 markets and 7 gears, with 18 variables including "
                "per-species biomass, gross catch, live and dead discards, "
                "price and sales value. The served tables carry five species in "
                "one sea and no fleet dimension at all, and the gridded file on "
                "the model server holds one size class and stops in 2009, which "
                "is why a case-study question about a recent year returns "
                "nulls that read as a broken tool rather than as an absence."
            ),
            lives_in=(
                "surimi-mcp/data/netcdf-samples/"
                "westmed-2013-2017-biomass-catch-sales.nc"
            ),
        ),
        capabilities.CannotAnswer(
            question_class=(
                "how uncertain a number is, or what confidence to place in it"
            ),
            why="not_collected",
            detail=(
                "Whether a dataset is observed or modelled is answered: every dataset "
                "declares kind, and aggregate and query_metric return data_kind "
                "beside the number with a disclosure note when it is modelled. "
                f"UNCERTAINTY is carried by 3 of the {served_count()} served datasets, "
                "not by the rest. cv_uncertainty on the empirical biomass survey "
                "(60 rows), and published confidence intervals on both ICES stock "
                "assessment tables. For any OTHER number this server can state "
                "provenance and cannot state confidence. On the ICES tables "
                "(latest and timeseries): low_stock_size and high_stock_size "
                "are the published confidence interval on a stock estimate, and "
                "low_fishing_pressure / high_fishing_pressure the same for F. Quote "
                "those as bounds on THAT stock's estimate only; they say nothing about "
                "the confidence of any other served number, and quoting them as such "
                "is a fabrication."
            ),
            lives_in="data/exports/hive_surimi_surimi_empirical_biomass_surveys_western_mediterranean.csv",
        ),
    ),
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="indicators",
    title="Indicator definitions and metadata",
    domain="reference",
    description="Definitions, units, and sources for fisheries indicators.",
    tools=("explain_indicator", "explain_species_code", "data_list_functional_groups"),
    coverage={},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="documents",
    title="SURIMI project documents",
    domain="reference",
    description=(
        "BM25 full-text (verbatim) search over SURIMI documents: public project wiki, "
        "gRPC protocol, model repos, data-lake documents. CPU-only "
        "(SQLite FTS5); returns exact passages + source so answers stay grounded."
    ),
    tools=("search_documents",),
    coverage={"documents": "references/, including the public project wiki (md/txt/rst/pdf); the internal development wiki is never indexed"},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="models",
    title="Fisheries & ecosystem modelling frameworks",
    domain="reference",
    description=(
        "Grounded descriptions of the modelling frameworks in the SURIMI ecosystem "
        "(Ecopath with Ecosim / EwE, MICE, POSEIDON, CMSY/CMSY++): what each is, who built "
        "it, what it does. General background to answer 'what is X' accurately instead "
        "of fabricating provenance or asserting X is 'not used in SURIMI' from a search miss."
    ),
    tools=("describe_models",),
    coverage={"models": len(models.MODELS)},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="recipes",
    title="Server-run analyses",
    domain="analysis",
    description="Analyses the server runs itself, returning a finding with its calculation records.",
    tools=("analyze_trend", "compare_countries", "generate_report", "explain_provenance", "breakdown"),
    coverage={},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="cube",
    title="Deterministic aggregation engine (DuckDB cube)",
    domain="analysis",
    description=(
        "Exact GROUP BY aggregation over the typed DuckDB cube built from the "
        "data lake: sum / avg / count / min / max / count_distinct, grouped, "
        "filtered, ordered. The engine computes every number (the LLM never does) "
        "and signs it in a calculation record. Call `aggregate` for ANY total, "
        "average, count, ranking, min/max or share. Never eyeball a number "
        "from rows. NULL group keys are excluded (not bucketed as 'Unknown'); "
        "EAV tables require a single variable_code filter before a magnitude sum."
    ),
    tools=("aggregate", "describe_quality"),
    coverage={"engine": "embedded DuckDB", "datasets": "data-lake CSVs (typed)"},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="metrics",
    title="Named metric registry",
    domain="analysis",
    description=(
        "Safe, named metrics that own their table, aggregation, units and time rule. "
        "Prevents wrong-table selection and accidental cross-year summing of stock "
        "metrics. A successful result carries a calculation record. Use `query_metric` for any number "
        "that has a named metric; use `aggregate` for ad-hoc GROUP BY."
    ),
    tools=("query_metric", "list_metrics"),
    coverage={"metrics": len(metrics_module.REGISTRY)},
))

capabilities.register(capabilities.DataSourceCapability(
    source_id="meta",
    title="Self-description and verification of calculation records",
    domain="meta",
    description="Capability introspection for client discovery, and verification of calculation records.",
    tools=("describe_data_capabilities", "verify"),
    coverage={},
))
