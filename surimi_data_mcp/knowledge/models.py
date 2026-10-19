"""Reference entries for the SURIMI model ensemble and the interoperability layer, returned by `describe_models`.

`MODELS` holds one entry per model or runtime component. Each entry has a `one_liner`, a general description in `what_it_is`, the SURIMI-specific part in `surimi_role`, and a `sources` list that names the reference document, repository or file behind it. The text of an entry marked "general background" comes from outside the SURIMI references.

`COUPLING` describes how the models exchange data. The models run as separate container services, and the Controller passes all data between them over gRPC, one time step at a time.

`GROUNDING` and `SURIMI_ARCHITECTURE` are returned with the entries and are read by the LLM. To change an entry, edit it here and cite its source.
"""
from __future__ import annotations

from typing import Any, Optional

GROUNDING = (
    "Grounded in the SURIMI references (Grant Agreement Part B, the project wiki, the "
    "consortium repositories and the surimi.v1 gRPC contracts). See `sources`. State the "
    "general facts and the cited SURIMI role. Do not add versions, provenance or couplings "
    "beyond what is written. If asked for detail that is not covered, say so and offer "
    "search_documents."
)

SURIMI_ARCHITECTURE = (
    "SURIMI builds a Digital Twin of the Ocean (DTO) by connecting a model ensemble "
    "through a shared interoperability layer, not through bespoke point-to-point code. "
    "The Grant Agreement lists the model ensemble and its deliverables (D3.1 to D3.9). "
    "The socio-economic models are a system dynamics model (D3.1, NORCE), a product-flow value chain (D3.2, EII) "
    "and the agent-based POSEIDON (D3.3, UOXF). The ecological models are MICE (D3.4, EII) and Ecopath with Ecosim, EwE (D3.5, EII). "
    "The assessment layer is CMSY++ (D3.6, UOXF) and an NLP assessment model (D3.7, NORCE). "
    "The policy layer, which is the management strategy evaluation (MSE) layer, uses either POSEIDON management procedures "
    "or an R library such as openMSE (D3.8, UOXF), together with an NLP policy model (D3.9, NORCE). "
    "The interoperability layer is a protocol (D4.3) plus software (D4.4), both led by EII. "
    "The graphical interfaces are the MSE decision support tool (D4.5) and the data GUI (D4.2), both led by KONNECTA. "
    "At runtime the models are Docker services on EDITO, connected hub-and-spoke through a Controller (see COUPLING). "
    "The NLP assessment model (D3.7) is the SURIMI-data-mcp server and the NLP policy model (D3.9) is the SURIMI-model-mcp server. "
    "The chat and the language model service are supporting parts and not deliverables."
)

# Answer to "how are the SURIMI models connected" and "how does EwE connect to
# POSEIDON". `describe_models` returns it for a name that `_is_coupling_query` accepts.
COUPLING = (
    "SURIMI models are separate container services connected through a shared "
    "protocol and one Controller. A model never calls or resolves another model itself. "
    "There are two pieces.\n"
    "(1) The contract. The SURIMI-protocol repository holds the *.proto files (protobuf package "
    "surimi.v1). They are built and linted with Buf and pushed to the Buf Schema Registry, which "
    "generates the SDK for each language that the models use: Java for POSEIDON, Python for CMSY++, "
    "the Market and the Output Creator, and C# for EwE, the Controller, the Fisheries Authority, "
    "the Environment and the Value Chain. All models share one schema, so no two models are coupled "
    "to each other directly.\n"
    "(2) The runtime. The containers talk gRPC over HTTP/2, and only the Controller talks to the "
    "models. It passes the data between them. Each model implements one service of the protocol: "
    "EcologyService (EwE), FisheryService (POSEIDON), FisheriesAuthorityService, MarketService, "
    "EnvironmentService, StockAssessmentService (CMSY++), OutputCreatorService and ValueChainService. "
    "The Controller itself implements ControllerService (SubmitExperiment, RemoveExperiment, "
    "GetAllSimulationStatuses, GetSimulationContract, GetProtocolVersion), which the GUI calls. "
    "One experiment runs as several parallel simulations. At the start the Controller sends every model "
    "the Simulation contract, which it reads from the scenario's YAML file (area, resolution, time step, "
    "vocabularies and the lists of species, markets and fleets). A model that cannot honour the contract "
    "throws an error.\n"
    "So 'how does EwE connect to POSEIDON?' Each simulation runs one time step at a time. "
    "In a step the Controller first reads the species prices from the Market (GetSpeciesPrices) and sends "
    "them to POSEIDON and EwE (UpdateSpeciesPrices). It simulates the step in EwE (SimulateStep), reads "
    "the biomass from EwE (GetBiomass) and sends it to POSEIDON (UpdateBiomass). It then simulates the "
    "step in POSEIDON, reads POSEIDON's catch disposition (GetCatchDisposition) and sends it to EwE "
    "(UpdateCatchDisposition). EwE and POSEIDON are coupled in every step, but always through the Controller. "
    "The Controller then reads the biomass and the catch disposition from EwE and sends them to the "
    "Fisheries Authority (UpdateBiomass, UpdateCatchDisposition). It reads the sales from EwE and POSEIDON "
    "(GetSales) and sends them to the Market (UpdateSales), which uses them to calculate the next prices. "
    "In the first step of each year it also reads the regulations from the Fisheries Authority "
    "(GetRegulations) and sends them to EwE and POSEIDON (UpdateRegulations).\n"
    "After each step the Controller waits until all parallel simulations have reported for that date. "
    "It calculates the mean and the 5th and 95th percentile of each value and sends these statistics to "
    "the experiment-level services. CMSY++ and the Output Creator receive the biomass and catch statistics. "
    "The Output Creator also receives the fishing activity, sales and price statistics, and the Value Chain "
    "receives the sales statistics. When the experiment ends, the Controller asks CMSY++ for the stock "
    "assessment (GetStockAssessment) and passes it to the Output Creator, which writes the output file of "
    "the experiment.\n"
    "The Environment service and EcologyService.UpdateEnvironmentVariables exist in the protocol. "
    "The Controller code (commit of 2026-10-07) has no call to the Environment service.\n"
    "The Value Chain sits outside the price loop. The Controller sends the market prices to POSEIDON "
    "and EwE only, which value their landings at those prices. The Controller then sends the aggregated "
    "sales to the Value Chain as sales statistics with quantity and value per species, fleet segment and "
    "market (UpdateSalesStatistics). Market prices therefore reach the Value Chain indirectly, through the "
    "values, and no price series is passed. The handler UpdateSalesStatistics calls UpdateSales() in "
    "EwEController.cs, which takes no argument and returns true. The statistics are discarded, and nothing "
    "in the Value Chain responds to a price change yet.\n"
    "The Grant Agreement cites an earlier coupling of POSEIDON with OSMOSE as prior art, which needed "
    "changes in the code of both models. The SURIMI protocol is designed to avoid that."
)

_GA = "references: SURIMI Grant Agreement 101157456 Part B"
_WIKI = "references: SURIMI-project.wiki"
_PROTO = "references: SURIMI-protocol/surimi/v1/*.proto (package surimi.v1)"

MODELS: list[dict[str, Any]] = [
    # Ecological layer
    {
        "id": "ewe",
        "name": "Ecopath with Ecosim (EwE)",
        "aka": ["ewe", "ecopath", "ecosim", "ecospace", "ecopath with ecosim", "surimiecopath"],
        "kind": "ecosystem model", "layer": "ecological",
        "one_liner": "Open-source ecosystem modelling suite (mass-balance food web, change over time, and space). SURIMI's ecology operating model.",
        "what_it_is": (
            "A free, open-source ecosystem modelling suite that represents an ecosystem as a "
            "network of functional groups and the biomass and energy flows between them. "
            "Its components are Ecopath (a static mass-balance snapshot), Ecosim (a simulation over "
            "time) and Ecospace (a spatially explicit grid). It is used for ecosystem-based fisheries management."
        ),
        "developer": (
            "The Ecopath International Initiative (EII) maintains EwE. SURIMI-Ecopath uses the "
            "NuGet package `Eii.Ecopath.EwECore` (version 6.7.*) and wraps the EwE `cCore`, with its "
            "EcopathDataStructures, EcosimModelParameters and EcospaceDataStructures. "
            "[General background, not in the consortium repos: EwE traces to Polovina's Ecopath (1984) and to "
            "Christensen, Walters and Pauly at the University of British Columbia.]"
        ),
        "surimi_role": (
            "SURIMI's ecological operating model, deliverable D3.5 (lead EII): 'a complex "
            "implementation of Ecopath with Ecosim'. SURIMI-Ecopath is a C# (.NET) gRPC service that wraps "
            "EwE Core and runs Ecospace one time step at a time on the Controller's clock. It implements "
            "the protocol's EcologyService. The Controller reads biomass, catch disposition, sales and "
            "fishing activity from it, and sends it POSEIDON's catch disposition, species prices and "
            "regulations. The repository bundles two EwE models, northwestern_med and anchovy_bay "
            "(Includes/). One Ecopath instance runs one simulation at a time, so the Controller routes "
            "each simulation of an experiment to its own instance. The service listens on port 7890 "
            "(image surimiecopath). MICE (D3.4) is the other ecological model in the Grant Agreement."
        ),
        "sources": [
            "references: SURIMI-Ecopath repository: SURIMI-Ecopath.csproj `Eii.Ecopath.EwECore` 6.7.*; EwE/Wrapper/EwECore.cs (cCore: EcopathDataStructures, EcosimModelParameters, EcospaceDataStructures); Includes/northwestern_med and Includes/anchovy_bay; ecopath_architecture.md",
            "references: SURIMI-anchovy_bay repository (Anchovy Bay EwE case study)",
            f"{_GA} (D3.5 'EwE model'; 'Ecopath and Ecosim (EwE) approach')",
            f"{_WIKI} (EDITO-and-Kubernetes service table, port 7890; ContextDiagram) + SURIMI-controller controller_architecture.md (SimulationDispatcher, one simulation per Ecopath instance) + {_PROTO} (ecology_service.proto)",
            "general background (not in consortium repos): EII ecopath.org; Christensen & Walters (2004), Ecological Modelling 172",
        ],
        "disambiguation": None,
    },
    {
        "id": "mice",
        "name": "MICE (Model of Intermediate Complexity for Ecosystem assessment)",
        "aka": ["mice", "model of intermediate complexity"],
        "kind": "ecosystem model", "layer": "ecological",
        "one_liner": "A simple multispecies model that is focused on one question, between single-species and full food-web models.",
        "what_it_is": (
            "A Model of Intermediate Complexity for Ecosystem assessment. It is a parsimonious "
            "dynamic model that includes only the species and processes relevant to the management "
            "question, and it can be estimated statistically by fitting it to data. It sits between single-species "
            "stock assessment and full-complexity ecosystem models like EwE."
        ),
        "developer": "An approach formalised in the literature (for example Plaganyi et al., 2014, Fish and Fisheries 15).",
        "surimi_role": (
            "SURIMI's other ecological-layer model, deliverable D3.4 (lead EII): 'a simple "
            "temporal food web model of intermediate complexity (MICE)'. The Grant Agreement pairs it with EwE "
            "so that the ensemble spans simple to complex models and validates the interoperability protocol. "
            "MICE is a Grant Agreement deliverable. It is not part of the demonstrated "
            "runtime stack: no consortium repository, wiki page or protocol service mentions it, "
            "and the EDITO service table lists no MICE service. Do not say that MICE is currently "
            "wired into the running system."
        ),
        "sources": [
            f"{_GA} (D3.4 'MICE ecosystem model'; 'simple temporal food web model of intermediate complexity (MICE)')",
            "references: no MICE repository, wiki page or protocol service exists among the consortium repositories (SURIMI-Ecopath, CMSY, POSEIDON, market, environment, value-chain, fisheries-authority, controller, output-creator, protocol, project.wiki), and the EDITO service table lists no MICE service. It is a Grant Agreement deliverable, not a built or wired model.",
            "general background (not in consortium repos): Plaganyi et al. (2014), Fish and Fisheries 15(1)",
        ],
        "disambiguation": None,
    },
    # Assessment layer
    {
        "id": "cmsy",
        "name": "CMSY / CMSY++",
        "aka": ["cmsy", "cmsy++", "cmsypp", "catch-msy", "catch msy", "catchmsy"],
        "kind": "stock assessment method", "layer": "assessment",
        "one_liner": "Catch-only, data-limited method that estimates MSY and stock status from a catch series plus resilience priors.",
        "what_it_is": (
            "A data-limited stock assessment method. From a catch time series plus priors on "
            "resilience (productivity) and on the start and end depletion, it estimates MSY and reference "
            "points (biomass, exploitation, status). It finds the viable r-k pairs of a Schaefer "
            "surplus-production model, and CMSY++ adds a Bayesian Schaefer Model (BSM), fitted with "
            "JAGS, when a CPUE or biomass index is available."
        ),
        "developer": (
            "The R script AA_CMSY++.R credits Rainer Froese, Gianpaolo Coro and Henning Winker, and the "
            "Grant Agreement cites Froese et al. (2021), User guide for CMSY++. "
            "[General background, not in the consortium repos: 'CMSY' is Catch-MSY, not 'Catch-Multiple-Standard-Yield'. "
            "The method is described in Froese, Demirel, Coro, Kleisner & Winker (Fish and Fisheries, 2017).]"
        ),
        "surimi_role": (
            "SURIMI's assessment model, deliverable D3.6 (lead UOXF): 'seamless connection via R "
            "libraries'. The SURIMI-CMSY repository is a Python gRPC service around the "
            "`AA_CMSY++.R` model. It implements StockAssessmentService on port 5021 (InitialiseExperiment, "
            "ExperimentStep, UpdateBiomassStatistics, UpdateCatchDispositionStatistics, GetStockAssessment, "
            "FinaliseExperiment, CancelExperiment) and supports monthly steps only (P1M). "
            "The Controller sends it the biomass and catch statistics that it calculated over all parallel simulations, "
            "and at the end it asks for the stock assessment and passes that to the Output Creator. "
            "The service runs in two phases, a historical assessment from the filtered catch data and then a "
            "simulated phase that forces btype=CPUE. It uses JAGS for the Bayesian Schaefer fit and a "
            "feed-forward neural network prior (`ffnn.bin`). Its inputs are catch_file.csv (Stock, yr, ct, bt) and "
            "id_file.csv, with FAO division and ASFIS common-name lookups. The R script runs with plots and PDF "
            "output switched off (`save.plots <- F`, `write.pdf <- F`). SURIMI's "
            "GSA06 (Northern Spain) inputs are built from Spanish regional landings (CSIC), MEDITS "
            "CPUE, the ASFIS taxonomy and FishBase priors. "
            "Starting and ending biomass prior: id_file.csv has the expert columns stb.low, stb.hi "
            "and endb.low, endb.hi (depletion bounds at the start and end of the series). AA_CMSY++.R only "
            "uses them when neither bound of a pair is NA (line ~943). All 153 GSA06 stocks in the input file "
            "leave stb.low and endb.low blank, so the historical assessment uses the automatic, "
            "catch-driven rule instead. When the ratio of early or late catch to the "
            "estimated MSY reaches 1.21 (ct_MSY.lim), the code skips its neural-network prior and sets "
            "that end's biomass prior to the fixed "
            "range 0.256-0.721 of unfished biomass (bk.MSY). This range straddles a typical "
            "Bmsy proxy and is not an explicit call that the stock is depleted. A run that opens on a declining "
            "biomass trend with no expert prior set reflects this rule and not a modelled decline "
            "in the underlying stock. SURIMI runs no experiment that separates fishing pressure "
            "from climate forcing, so the rule explains the starting prior and not "
            "the cause of an observed catch decline."
        ),
        "sources": [
            "references: SURIMI-CMSY repository: README.md; CMSY_architecture.md; server/app.py and server/StockAssessment.py (StockAssessmentService, two-phase flow, port 5021, P1M); R_files/AA_CMSY++.R (JAGS, ffnn.bin, save.plots and write.pdf off)",
            "references: SURIMI-CMSY/R_files/AA_CMSY++.R (ct_MSY.lim <- 1.21 at line ~85; bk.MSY <- c(0.256, 0.721) at line ~810; startbio and endbio branches at lines ~815-850; use of the expert bounds at lines ~943-947)",
            "references: SURIMI-DATA-LAKE/9_Model Inputs/CMSY++/GSA06_CMSY++_input files/ (catch_file.csv, id_file.csv, README.txt). All 153 GSA06 rows have stb.low, stb.hi, endb.low and endb.hi empty.",
            f"{_GA} (D3.6 'CMSY++ assessment model'; 'seamless connection via R libraries'; Froese et al. 2021, User guide for CMSY++) + {_PROTO} (stock_assessment_service.proto) + SURIMI-controller ExperimentManager.cs",
            "general background (not in consortium repos): Froese et al. (2017), Fish and Fisheries 18(3)",
        ],
        "disambiguation": None,
    },
    {
        "id": "nlp-assessment",
        "name": "NLP assessment model (D3.7), the SURIMI-data-mcp server",
        "aka": ["nlp assessment", "d3.7", "assessment nlp", "surimi assistant", "results assessment"],
        "kind": "nlp / ai module", "layer": "assessment",
        "one_liner": "An LLM-based module that turns numerical observations into concise textual ocean-health and economic assessments. SURIMI delivers it as SURIMI-data-mcp.",
        "what_it_is": (
            "An NLP/AI module that uses an LLM to interpret the numerical observations produced "
            "by the socio-ecological models and translate them into concise textual summaries "
            "(ocean health and economic value), with adjustable point of view, tone and KPIs."
        ),
        "developer": "SURIMI deliverable D3.7, lead NORCE.",
        "surimi_role": (
            "Deliverable D3.7 (NORCE): 'we will employ a LLM to interpret the numerical "
            "observations from the socio-ecological models and translate them into concise, "
            "textual summaries'. SURIMI-data-mcp, the server that returns this answer, is the D3.7 model. "
            "The chat (SURIMI-vllm-ui) and the language model service (SURIMI-vllm) are supporting parts "
            "and not part of the deliverable. The Grant Agreement says that the text-based models will "
            "operate as any other in the MSE cycle through the interoperability layer. The Controller "
            "calls no NLP service, so the delivered servers work outside the simulation loop. "
            "According to the Grant Agreement, the NLP policy model (D3.9) acts on the textual summaries."
        ),
        "sources": [
            f"{_GA} (D3.7 'NLP assessment model'; 'employ a LLM to interpret the numerical observations ... concise, textual summaries'; lead NORCE) + references: SURIMI-mcp README (the server is the deliverable D3.7) + SURIMI-controller controller_architecture.md (outbound calls)",
        ],
        "disambiguation": None,
    },
    # Policy / MSE layer
    {
        "id": "poseidon",
        "name": "POSEIDON (agent-based fisheries model)",
        "aka": ["poseidon"],
        "kind": "agent-based bio-economic fisheries model", "layer": "socio-economic / policy",
        "one_liner": "Agent-based, spatially explicit bio-economic model of fishers' decisions. SURIMI's fishery operating model (D3.3) and an MSE policy option (D3.8).",
        "what_it_is": (
            "An agent-based, spatially explicit bio-economic simulation of fisheries. Individual "
            "vessels are agents that choose where and when to fish and with which gear, in response to "
            "profit, fuel costs, regulations and stock distribution. It is used to study the emergent behaviour "
            "of fleets and how a fishery responds to management measures."
        ),
        "developer": (
            "The Grant Agreement names UOXF as lead of the agent-based model (D3.3) "
            "and cites Bailey et al. (2019), Sustainability Science 14:259-275, for POSEIDON. "
            "Nicolas Payette is the author of the commits in the SURIMI-POSEIDON repository. "
            "[General background, not in consortium repos: there is also an "
            "unrelated HCMR 'POSEIDON' ocean-observing system. See disambiguation.]"
        ),
        "surimi_role": (
            "Dual deliverable role. D3.3 (lead UOXF) is 'a comprehensive fishery agent-based model', the "
            "socio-economic and fishery operating model. D3.8 allows the policy/MSE layer to 'extract "
            "management procedures from the POSEIDON model' (or to use a pre-existing R library such "
            "as openMSE). SURIMI wraps the model as SURIMI-POSEIDON, a Java/Gradle gRPC server "
            "around the POSEIDON code (a git submodule of poseidon-fisheries/POSEIDON). Its scenario is "
            "northwestern_med, the Spanish bottom trawlers and purse seiners of the northwestern Mediterranean "
            "from 2013. The scenario data come from the SURIMI data-preprocessing pipelines and are stored in the "
            "SURIMI-POSEIDON_inputs repository. At runtime POSEIDON implements the protocol's FisheryService, "
            "the fisher and effort model. The Controller sends it biomass (from EwE), species prices (from the Market) "
            "and regulations (from the Fisheries Authority). It reads catch disposition, sales and fishing "
            "activity from it, and it passes the catch disposition on to EwE in the same time step. "
            "The service listens on port 50051 (image surimiposeidon). EwE and POSEIDON do not resolve each other internally. "
            "They are coupled in every time step through the Controller (see describe_models('coupling'))."
        ),
        "sources": [
            "references: SURIMI-POSEIDON repository: README.md, AGENTS.md and POSEIDON_architecture.md (Java/Gradle gRPC server, northwestern_med scenario, port 50051, inputs submodule SURIMI-POSEIDON_inputs)",
            "general background (public upstream): github.com/poseidon-fisheries/POSEIDON; Bailey et al. (2019), Sustainability Science 14:259-275 (cited in the Grant Agreement)",
            f"{_GA} (D3.3 'POSEIDON, a comprehensive fishery agent-based model', lead UOXF; D3.8 'management procedure component of POSEIDON ... or a pre-existing R library (such as openMSE)'; POSEIDON<->OSMOSE prior art)",
            f"{_WIKI} (EDITO-and-Kubernetes service table) + SURIMI-controller SimulationManager.cs (calls to POSEIDON) + {_PROTO} (fishery_service.proto)",
        ],
        "disambiguation": (
            "'POSEIDON' names several unrelated marine systems. SURIMI's POSEIDON is the agent-based "
            "fisheries model of the Grant Agreement (D3.3, UOXF). [General background, not in the SURIMI "
            "references: there is also an HCMR 'POSEIDON' Greek ocean-observing and forecasting system, "
            "which is unrelated.]"
        ),
    },
    {
        "id": "nlp-policy",
        "name": "NLP policy model (D3.9), the SURIMI-model-mcp server",
        "aka": ["nlp policy", "d3.9", "policy nlp", "ai management procedure"],
        "kind": "nlp / ai module", "layer": "policy",
        "one_liner": "An LLM-based policy module. In the Grant Agreement it adjusts management practices from the textual assessment and justifies the decision. SURIMI delivers it as SURIMI-model-mcp.",
        "what_it_is": (
            "An NLP/AI policy module that adjusts management practices (for example fishing-season "
            "duration or trip-length restrictions) based solely on the textual assessment summaries "
            "from D3.7, and gives a justification for the decision taken."
        ),
        "developer": "SURIMI deliverable D3.9, lead NORCE.",
        "surimi_role": (
            "Deliverable D3.9 (NORCE), the policy-layer NLP: 'will adjust management practices, "
            "such as fishing season duration or trip length restrictions, based solely on these "
            "textual summaries, providing a justification of the decision taken'. "
            "The Grant Agreement describes a text-to-text hand-off from the D3.7 assessment module. "
            "The policy module acts on the assessment's prose and not on the raw numbers. "
            "SURIMI-model-mcp, the server that answers questions about the simulation runs and the "
            "gridded model output, is the D3.9 model. The Controller calls no NLP service."
        ),
        "sources": [
            f"{_GA} (D3.9 'NLP policy model'; 'adjust management practices ... based solely on these textual summaries, providing a justification'; lead NORCE)",
            "references: SURIMI-netcdf-mcp README (the server is the deliverable D3.9)",
        ],
        "disambiguation": None,
    },
    # Socio-economic layer
    {
        "id": "system-dynamics",
        "name": "System dynamics model (D3.1)",
        "aka": ["system dynamics", "d3.1", "differential equation model"],
        "kind": "system-dynamics model", "layer": "socio-economic",
        "one_liner": "A straightforward system-dynamics (differential-equation) model of price, cost and effort dynamics.",
        "what_it_is": (
            "A socio-economic system-dynamics (differential-equation) model that simulates price "
            "dynamics (short and long-term elasticities), cost dynamics (harvesting effort) and effort "
            "dynamics (how exploitation adapts to regulation)."
        ),
        "developer": "SURIMI deliverable D3.1, lead NORCE.",
        "surimi_role": (
            "Deliverable D3.1 (NORCE): 'a straightforward system dynamics/differential equation "
            "model'. It is one of three socio-economic models, with the product-flow value chain (D3.2) and "
            "the agent-based POSEIDON (D3.3). The Grant Agreement says all three simulate price, cost and effort dynamics."
        ),
        "sources": [f"{_GA} (D3.1 'System dynamics model'; '1. A straightforward system dynamics/differential equation model'; lead NORCE)"],
        "disambiguation": None,
    },
    {
        "id": "value-chain",
        "name": "Value Chain model (product-flow value chain, D3.2)",
        "aka": ["value chain", "valuechain", "surimivaluechain", "product flow", "supply chain", "d3.2"],
        "kind": "economic model", "layer": "socio-economic",
        "one_liner": "Product-flow and supply-chain model. It receives sales statistics at the end of the chain (D3.2).",
        "what_it_is": (
            "A product-flow value-chain model. According to its repository it 'models the effect of "
            "fisheries on other industries, like processing factories, distribution, wholesale, "
            "ports, restaurants'. For example, catching anchovy benefits the tinning industry, wholesale and "
            "restaurants as well as the fishery."
        ),
        "developer": "SURIMI deliverable D3.2, lead EII (SURIMI-value-chain repository, C#).",
        "surimi_role": (
            "Deliverable D3.2 (EII), the product-flow value-chain model (SURIMI-value-chain repository). "
            "At runtime it implements the protocol's ValueChainService and receives the sales statistics "
            "(UpdateSalesStatistics). The wiki describes its output as a time series that is still to be defined. "
            "The service listens on port 7990 (image surimivaluechain). "
            "It receives no price series. The Controller (SimulationManager.cs) sends updated "
            "species prices to POSEIDON and EwE only, in every step. Those models value their "
            "landings at the prices, and the Controller then sends the aggregated sales to the "
            "value chain as sales statistics with quantity and value per species, fleet "
            "segment and market (UpdateSalesStatistics, sales_statistics.proto), so prices reach "
            "it indirectly through the values. The receiving side is not built yet. The handler "
            "UpdateSales() in SURIMI-value-chain's EwEController.cs takes no argument and "
            "returns true, so the statistics are discarded and no output is produced. "
            "UpdateSalesSummariesAsync() and GetCatchDispositionSummaryAsync() throw "
            "NotImplementedException, but the sales do not take that path."
        ),
        "sources": [
            "references: SURIMI-value-chain repository: README ('effect of fisheries on other industries ... tinning industry, wholesale, restaurants')",
            "references: SURIMI-value-chain/SURIMI-value-chain/EwE/EwEController.cs (UpdateSales() takes no argument and returns true; UpdateSalesSummariesAsync and GetCatchDispositionSummaryAsync throw NotImplementedException) and Services/ValueChainService.cs (UpdateSalesStatistics calls UpdateSales())",
            "references: SURIMI-controller/SURIMI-controller/Services/SimulationManager.cs (UpdateSpeciesPricesAsync sends to the POSEIDON and Ecopath clients only) and ExperimentManager.cs (UpdateSalesStatistics to the Output Creator and the Value Chain)",
            f"{_GA} (D3.2 'Product flow value-chain model'; lead EII)",
            f"{_WIKI} (EDITO-and-Kubernetes service table, port 7990; SURIMI-Output, Value chain section) + {_PROTO} (value_chain_service.proto, sales_statistics.proto)",
        ],
        "disambiguation": None,
    },
    # Prior art and context
    {
        "id": "osmose",
        "name": "OSMOSE (prior-art ecosystem model)",
        "aka": ["osmose"],
        "kind": "ecosystem model (prior art / context)", "layer": "context",
        "one_liner": "Individual-based multispecies ecosystem model. The Grant Agreement cites its earlier coupling with POSEIDON as the case that motivated SURIMI's universal protocol.",
        "what_it_is": (
            "An individual-based, multispecies marine ecosystem model. It is not a SURIMI deliverable. "
            "The Grant Agreement cites it as prior art: the earlier point-to-point integration of POSEIDON with OSMOSE "
            "needed changes to the code of both models, which is the kind of bespoke "
            "coupling that SURIMI's interoperability protocol is meant to replace."
        ),
        "developer": "External. The Grant Agreement cites Grüss et al. (2016), Ecological Modelling 340:86-105, on management strategy evaluation with OSMOSE.",
        "surimi_role": (
            "Context only, not a SURIMI deliverable. The Grant Agreement cites POSEIDON<->OSMOSE as an "
            "example of point-to-point coupling that 'necessitates modifications to the "
            "underlying code of both models'. This motivates SURIMI's interoperability layer."
        ),
        "sources": [f"{_GA} ('integration of the fishery agent-based model POSEIDON with the ecosystem model OSMOSE ... necessitates modifications to the underlying code of both models')"],
        "disambiguation": None,
    },
    # Runtime and interoperability components (not scientific models, but part of the system)
    {
        "id": "controller",
        "name": "SURIMI Controller (ControllerService)",
        "aka": ["controller", "surimicontroller", "controllerservice", "simulation controller"],
        "kind": "runtime service (orchestrator)", "layer": "interoperability",
        "one_liner": "The hub. It is the only component that talks to the models, it steps every simulation and it routes all data between the models.",
        "what_it_is": (
            "The orchestration hub of the runtime. It is the only component that talks to the "
            "models, and the models never exchange data directly. It starts the parallel simulations of an experiment, "
            "runs each simulation one time step at a time and passes the data between the "
            "models (biomass, catches, sales and so on) by reading from one model and updating another. "
            "After each step it calculates the mean and the 5th and 95th percentile over the parallel simulations "
            "and sends these statistics to the experiment-level services."
        ),
        "developer": "The SURIMI-controller repository (C#, .NET). The Grant Agreement assigns the interoperability protocol and software to EII (D4.3, D4.4).",
        "surimi_role": (
            "Implements ControllerService (SubmitExperiment, RemoveExperiment, "
            "GetAllSimulationStatuses, GetSimulationContract, GetProtocolVersion), whose commands typically "
            "come from the GUI. It reads the Simulation contract from the scenario's YAML file and sends it to the "
            "models when a simulation or experiment is initialised. It is the only service that does not "
            "belong to the model side of the protocol: it orchestrates and does not simulate. "
            "The service listens on port 5092 (image surimicontroller). See describe_models('coupling') "
            "for the order of the calls."
        ),
        "sources": [
            "references: SURIMI-controller repository (C#): SURIMI-controller/Services/SimulationManager.cs and ExperimentManager.cs; controller_architecture.md",
            f"{_WIKI} (ContextDiagram 'Controller ... pass all data between all models'; controller_architecture.md; EDITO-and-Kubernetes service table, port 5092)",
            f"{_PROTO} (controller_service.proto SubmitExperiment, RemoveExperiment, GetAllSimulationStatuses, GetSimulationContract)",
        ],
        "disambiguation": None,
    },
    {
        "id": "output-creator",
        "name": "Output Creator (OutputCreatorService)",
        "aka": ["aggregator", "surimioutputcreator"],
        "kind": "runtime service (data sink)", "layer": "interoperability",
        "one_liner": "The result writer. It records the statistics that the Controller sends and produces the output file of the experiment.",
        "what_it_is": (
            "A Python gRPC server that produces the output files of a SURIMI experiment. The models "
            "write no files themselves. The Output Creator records the statistics messages (biomass, catch "
            "disposition, fishing activity, sales, species prices and the stock assessment) to a binary file. "
            "After the experiment is finalised it generates a NetCDF4 file and uploads it to S3."
        ),
        "developer": "The SURIMI-output-creator repository (Python, EUPL-1.2).",
        "surimi_role": (
            "Implements OutputCreatorService: InitialiseExperiment, ExperimentStep, FinaliseExperiment, "
            "CancelExperiment, the Update*Statistics calls and UpdateStockAssessment. It only receives calls "
            "from the Controller. The protocol defines no AggregatorService, and older documents use the "
            "name Aggregator for this role. The Dockerfile exposes port 5189 (image surimioutputcreator), "
            "and the wiki's EDITO table lists 5188 and 5189 for the Output Creator."
        ),
        "sources": [
            "references: SURIMI-output-creator repository (Python): README.md, output-creator_architecture.md, Dockerfile",
            f"{_WIKI} (output-creator_architecture.md; EDITO-and-Kubernetes service table) + SURIMI-controller ExperimentManager.cs (statistics sent to the Output Creator)",
            f"{_PROTO} (output_creator_service.proto)",
        ],
        "disambiguation": None,
    },
    {
        "id": "fishery-authority",
        "name": "Fisheries Authority (FisheriesAuthorityService)",
        "aka": ["fishery authority", "fisheries authority", "surimifisheriesauthority", "regulations provider"],
        "kind": "runtime service (regulator)", "layer": "interoperability",
        "one_liner": "The regulator. It creates harvest control regulations (TAC), serves them each year, and receives biomass and catch data.",
        "what_it_is": (
            "The regulatory component of the management loop. It creates harvest "
            "control regulations (Total Allowable Catches per species and fleet segment) from the scenario "
            "configuration, serves them at the start of each simulation year, and receives biomass and catch "
            "disposition updates."
        ),
        "developer": "The SURIMI-fisheries-authority repository (C#, .NET).",
        "surimi_role": (
            "Implements FisheriesAuthorityService (CreateRegulations, GetRegulations, UpdateBiomass, "
            "UpdateCatchDisposition, UpdateFishingActivity). The Controller calls CreateRegulations at the start of a "
            "simulation, calls GetRegulations in the first step of each year and sends the regulations to EwE "
            "and POSEIDON. In every step it sends the Fisheries Authority the biomass and the catch disposition "
            "from EwE. The Controller code does not call UpdateFishingActivity. "
            "The policy setting is TargetFishingMortality (biomass_limit, biomass_base, f_max): below "
            "biomass_limit no fishing is allowed, above biomass_base fishing is unrestricted, and f_max is the "
            "maximum allowed catch. The service listens on port 5493 (image surimifisheriesauthority)."
        ),
        "sources": [
            "references: SURIMI-fisheries-authority repository (C#): fisheries-authority_architecture.md",
            f"{_PROTO} (fisheries_authority_service.proto; regulation_definition.proto TargetFishingMortality)",
            f"{_WIKI} (fisheries-authority_architecture.md; EDITO-and-Kubernetes service table, port 5493) + SURIMI-controller SimulationManager.cs",
        ],
        "disambiguation": None,
    },
    {
        "id": "market",
        "name": "Market model (MarketService)",
        "aka": ["market", "market model", "surimimarket", "market provider"],
        "kind": "runtime service (economic model)", "layer": "interoperability",
        "one_liner": "The price producer. It provides species prices and consumes sales to calculate the prices of the next period.",
        "what_it_is": (
            "The economic price component. It models fish prices on local markets (ports, identified by "
            "UN/LOCODE), where the price varies with supply according to a species-specific elasticity, and on a "
            "global market that is the supply-weighted average of the active local markets. It provides species "
            "prices and consumes sales to calculate the prices of the next period."
        ),
        "developer": "The SURIMI-market repository (Python).",
        "surimi_role": (
            "Implements MarketService (GetSpeciesPrices, UpdateSales). In every step the Controller reads the "
            "species prices and sends them to POSEIDON and EwE. It sends the sales from EwE and POSEIDON to the "
            "Market, which uses them to calculate the prices of the next time period. "
            "The service listens on port 5001 (image surimimarket)."
        ),
        "sources": [
            "references: SURIMI-market repository (Python): README.md, market_architecture.md, Dockerfile",
            f"{_PROTO} (market_service.proto)",
            f"{_WIKI} (market_architecture.md; EDITO-and-Kubernetes service table, port 5001) + SURIMI-controller SimulationManager.cs",
        ],
        "disambiguation": None,
    },
    {
        "id": "environment",
        "name": "Environment service (EnvironmentService)",
        "aka": ["environment", "environment provider", "surimienvironment", "environmental forcing"],
        "kind": "runtime service (data source)", "layer": "interoperability",
        "one_liner": "Provides environmental variables (temperature, salinity, chlorophyll, current) per longitude and latitude for the ecology model.",
        "what_it_is": (
            "Provides environmental variables per longitude and latitude cell (for example sea surface or bottom "
            "temperature, salinity, chlorophyll concentration and current) that the ecology model can use "
            "as forcing in climate scenarios."
        ),
        "developer": "The SURIMI-environment repository (C#, .NET).",
        "surimi_role": (
            "Implements EnvironmentService.GetEnvironmentVariables, and the protocol lets the Controller pass the "
            "variables to EwE with EcologyService.UpdateEnvironmentVariables. The Controller code "
            "(commit of 2026-10-07) has no call to the Environment service, so the wiring is not "
            "confirmed in the current code. Variable codes differ between documents: the protocol gives "
            "sea_surface_temperature and chlorophyll_concentration as examples, and the SURIMI-environment README "
            "lists water temperature, current and salinity. The service listens on port 5839 "
            "(image surimienvironment)."
        ),
        "sources": [
            "references: SURIMI-environment repository (C#): README 'provides variables about the environment on a given timestamp. Like water temperature, current, salinity, etc.'",
            f"{_PROTO} (environment_service.proto, environment_variables.proto, ecology_service.proto)",
            f"{_WIKI} (EDITO-and-Kubernetes service table, port 5839) + SURIMI-controller Services (no call to the Environment service)",
        ],
        "disambiguation": None,
    },
    {
        "id": "gui",
        "name": "GUI (development GUI; future MSE Decision Support Tool, D4.5)",
        "aka": ["gui", "surimigui", "mse gui", "decision support tool", "d4.5"],
        "kind": "runtime service (tool / UI)", "layer": "interoperability",
        "one_liner": "Where a simulation starts. It submits the experiment to the Controller. A development GUI now, and later the MSE decision support tool (D4.5) with an NLP component.",
        "what_it_is": (
            "The surface where an experiment is started. It submits the experiment to the "
            "Controller and shows the status of the simulations. At present it is a simple development GUI, "
            "which the wiki says will be replaced by the MSE decision support tool GUI."
        ),
        "developer": "The development GUI (SURIMI-gui, Blazor WebAssembly) is in the SURIMI-controller repository. The MSE decision support tool GUI is deliverable D4.5 (lead KONNECTA).",
        "surimi_role": (
            "Calls the Controller's ControllerService (SubmitExperiment, RemoveExperiment, GetAllSimulationStatuses, "
            "GetSimulationContract). The proto describes it as 'called to send commands to the simulation controller. "
            "For example by a GUI'. The development GUI listens on port 5045 (image surimigui). "
            "According to the Grant Agreement, the MSE decision support tool GUI (D4.5, KONNECTA) "
            "'will include an NLP component to produce text explanations of the dynamics and the results'."
        ),
        "sources": [
            f"{_WIKI} (ContextDiagram 'simple development GUI ... replaced by the MSE GUI'; EDITO-and-Kubernetes service table, port 5045)",
            f"{_PROTO} (controller_service.proto)",
            f"{_GA} (D4.5 MSE Decision Support Tool GUI with NLP component, lead KONNECTA) + references: SURIMI-controller repository (SURIMI-gui)",
        ],
        "disambiguation": None,
    },
    {
        "id": "interoperability-layer",
        "name": "Interoperability layer (protocol D4.3 + software D4.4; surimi.v1 gRPC contract)",
        "aka": ["interoperability layer", "interoperability", "surimi-protocol", "surimi.v1", "buf", "dto", "protocol", "coupling"],
        "kind": "interoperability", "layer": "interoperability",
        "one_liner": "The shared schema and runtime that connect all SURIMI models: surimi.v1 protobuf via Buf, plus the hub-and-spoke Controller runtime.",
        "what_it_is": (
            "The mechanism that connects the model ensemble in a generic way and replaces bespoke "
            "point-to-point couplings. It has two parts. The protocol defines which variables are transferred, at "
            "which dimensionality and resolution, on a shared time schedule, with roles for "
            "up-scaling and down-sampling between models of different resolution. The software provides flexible "
            "scheduling, consistent data transfer and a container runtime."
        ),
        "developer": "SURIMI deliverables D4.3 (protocol) and D4.4 (software), lead EII.",
        "surimi_role": (
            "The protocol is realised in the SURIMI-protocol repository: *.proto files (protobuf package "
            "surimi.v1; csharp_namespace 'Grpc.Surimi', java_package 'eu.project.surimi'), built and "
            "linted with Buf and pushed to the Buf Schema Registry (buf.build/surimi/surimi-protocol), "
            "which generates the SDKs that the models use. Communication is "
            "gRPC over HTTP/2 between Docker containers on EDITO. Each model implements one service "
            "(EcologyService, FisheryService, FisheriesAuthorityService, MarketService, EnvironmentService, "
            "StockAssessmentService, OutputCreatorService, ValueChainService). Models that run a simulation have the "
            "calls InitialiseSimulation, SimulateStep, FinaliseSimulation and CancelSimulation. Models that work "
            "on a whole experiment have InitialiseExperiment, ExperimentStep, FinaliseExperiment and "
            "CancelExperiment. ControllerService is the entry point for the GUI. This is the answer to 'how are "
            "the models coupled'. See describe_models('coupling')."
        ),
        "sources": [
            f"{_GA} (D4.3 interoperability protocol and D4.4 software; 'protocol ... and the coded interoperability software ... runtime environment for model containers'; lead EII)",
            f"{_WIKI} (BUF-Schema-Registry; ContextDiagram 'GRPC ... HTTP/2')",
            f"{_PROTO} (package surimi.v1; the service files and README.md of the SURIMI-protocol repository)",
        ],
        "disambiguation": None,
    },
]


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _is_coupling_query(name: str) -> bool:
    """Return True when a name asks how the models are coupled, wired or connected."""
    n = _norm(name)
    if any(k in n for k in ("coupl", "wiring")):
        return True
    if "connect" in n and any(k in n for k in ("model", "poseidon", "ewe", "ecopath")):
        return True
    if "resolve" in n and "poseidon" in n:
        return True
    if "howaremodels" in n or "howdomodels" in n or "howmodelsconnect" in n:
        return True
    return False


def find_model(name: str) -> Optional[dict[str, Any]]:
    """Return the entry whose id, name or `aka` alias matches `name`, or None.

    An exact match on the normalised text is tried first, then a substring match.
    """
    if not name:
        return None
    n = _norm(name)
    for m in MODELS:  # exact id / name / aka first
        keys = {_norm(m["id"]), _norm(m["name"])} | {_norm(a) for a in m["aka"]}
        if n in keys:
            return m
    for m in MODELS:  # then substring / contains
        hay = _norm(m["name"]) + "".join(_norm(a) for a in m["aka"])
        if n and (n in hay or any(_norm(a) and _norm(a) in n for a in m["aka"])):
            return m
    return None


def _public(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k != "aka"}


def describe_models(name: Optional[str] = None) -> dict[str, Any]:
    """Return a summary list of the model entries (id, name, kind, layer, `one_liner`), or the full entry that matches `name`.

    A name such as 'coupling' returns `COUPLING` instead. A name with no entry returns `found` False and a note that the entry is missing from this module, which does not show that SURIMI lacks it.
    """
    if not name:
        return {
            "models": [
                {"id": m["id"], "name": m["name"], "kind": m["kind"],
                 "layer": m["layer"], "one_liner": m["one_liner"]}
                for m in MODELS
            ],
            "count": len(MODELS),
            "surimi_architecture": SURIMI_ARCHITECTURE,
            "grounding": GROUNDING,
            "note": (
                "Call describe_models(name=...) for detail (e.g. 'EwE', 'CMSY++', 'POSEIDON', "
                "'Controller', 'Fishery Authority', 'NLP assessment'); or "
                "describe_models(name='coupling') for how the models are connected."
            ),
        }
    if _is_coupling_query(name):
        return {"query": name, "topic": "coupling", "coupling": COUPLING,
                "surimi_architecture": SURIMI_ARCHITECTURE, "grounding": GROUNDING}
    m = find_model(name)
    if m is None:
        return {
            "query": name,
            "found": False,
            "known_models": [x["name"] for x in MODELS],
            "surimi_architecture": SURIMI_ARCHITECTURE,
            "note": (
                f"'{name}' is not in this model knowledge base. That means it is not documented "
                "here. It does not mean SURIMI does not use it. Offer to search project documents "
                "(search_documents) and give accurate general background only if you are sure of it. "
                "Do not invent provenance, versions or how SURIMI couples it."
            ),
        }
    return {"query": name, "found": True, "model": _public(m), "grounding": GROUNDING}
