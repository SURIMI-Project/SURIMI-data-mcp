# SURIMI data server: one-page cheat sheet

**What it is:** SURIMI-data-mcp is a tool server that lets an AI assistant answer questions from SURIMI's
fisheries data. Users ask in their own words, and the answers name their source and carry a calculation
record for every number. The server holds 28 datasets (EU fisheries economics, effort, capacity and
catches, FAO and ICES landings, ICES stock assessments, OECD fisheries support, survey data and SURIMI's
own tables) and searches the public SURIMI documents.

**No set-up needed in the SURIMI chat:** the chat on EDITO already uses the server, and you sign in there
with the EDITO button: <https://project-surimi-vllm-ui.lab.dive.edito.eu>. This sheet is for attaching the
server to your own assistant.

**Server address:**

```
https://project-surimi-surimi-data-mcp.lab.dive.edito.eu/mcp
```

---

## Connect it to Claude (a few steps)

**Claude Desktop or Claude.ai:**

1. Open **Settings → Connectors**.
2. Choose **Add custom connector**.
3. Give it a name, for example `SURIMI data`, and paste the server address above.
4. Start a new chat. The tools menu now lists the 20 SURIMI data tools.

**A client that only starts local servers:** use the `mcp-remote` bridge, which needs Node.js. In the
client's MCP configuration, add:

```json
{
  "mcpServers": {
    "surimi-data": {
      "command": "npx",
      "args": ["mcp-remote", "https://project-surimi-surimi-data-mcp.lab.dive.edito.eu/mcp"]
    }
  }
}
```

Other MCP clients (ChatGPT Connectors, Mistral Chat, opencode and others) attach in the same way, with the
same address.

SURIMI-model-mcp, the model server for SURIMI's simulation results, attaches in the same way at
`https://project-surimi-surimi-model-mcp.lab.dive.edito.eu/mcp`.

---

## Questions to try

These are questions SURIMI's fisheries experts asked:

1. "What data is available?"
2. "Which fishing sector is the most profitable in Italy?"
3. "For which species or groups are there biomass data in the Mediterranean Sea? Where are these data
   taken from?"
4. "The results I'm looking at show species only as three-letter codes, like PIL. Which species is this
   code referring to?"

The assistant picks the tools and the server calculates the numbers, each with a calculation record. To
check a record, open the server's audit page:
<https://project-surimi-surimi-data-mcp.lab.dive.edito.eu/audit>.

---

## If something looks off

| You see | What it means | Do this |
|---|---|---|
| The connector will not add | The address is probably wrong | Check that the address ends in `/mcp` and try again |
| No SURIMI tools in the chat | The connector is off for this chat | Turn it on in the chat's tools menu, or start a new chat |
| "The data does not hold this" | SURIMI's data cannot answer the question | This is a correct answer, not an error, and it often says where such data would normally be found |

Ask the assistant "What can this tool do?" for a quick self-check.

---

**More:** [tools.md](tools.md) lists each tool and what it does, and
[data_mcp_architecture.md](../data_mcp_architecture.md) describes how the server is built.
