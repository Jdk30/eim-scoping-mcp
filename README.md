# eim-scoping-mcp

Serveur MCP et application web locale pour mener une scoping review de façon traçable : recherche bibliographique, déduplication, tri à l'aveugle avec recommandations d'une IA, diagramme de flux, exports.

Le premier protocole fourni porte sur les **erreurs innées du métabolisme (EIM) en Afrique**. Le protocole est un fichier JSON : on peut l'adapter à un autre sujet sans toucher au code.

*English summary: an MCP server and a local web app for running a traceable scoping review (PubMed and Europe PMC search, deduplication, blind screening with AI recommendations kept separate from human decisions, flow-diagram counts, CSV/RIS export). The bundled protocol covers inborn errors of metabolism in Africa. The interface and documentation are in French for now.*

> **État : prototype de recherche.** Les recommandations de l'IA ne sont jamais des décisions : seules les décisions humaines entrent dans le diagramme de flux. Le protocole `protocol-v2.json` est une proposition non encore enregistrée ni relue par un spécialiste de l'information.

## Installation

Python 3.10 ou plus récent.

```bash
git clone https://github.com/Jdk30/eim-scoping-mcp.git
cd eim-scoping-mcp
pip install -r requirements.txt
```

Aucune clé d'API n'est nécessaire.

## Application web

```bash
python webapp.py
```

Puis ouvrir http://127.0.0.1:8765. L'application propose :

- un tableau de bord avec le diagramme de flux et les recherches exécutées ;
- le tri référence par référence (raccourcis `I` inclure, `U` incertain, `E` exclure, `N` suivante) ;
- la liste des références, filtrable ;
- l'accord entre vos décisions et les recommandations de l'IA (rappel, kappa de Cohen) ;
- le protocole et les exports CSV et RIS.

Le tri se fait à l'aveugle : la recommandation de l'IA ne s'affiche qu'après votre décision.

L'application n'écoute que sur la machine locale et n'a pas d'authentification. **Ne l'exposez pas telle quelle sur Internet.**

## Serveur MCP

Avec Claude Code :

```bash
claude mcp add eim-scoping -- python "/chemin/vers/eim-scoping-mcp/server.py"
```

Avec Claude Desktop, dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "eim-scoping": {
      "command": "python",
      "args": ["/chemin/vers/eim-scoping-mcp/server.py"]
    }
  }
}
```

| Outil | Rôle |
|---|---|
| `get_protocol` | Objectifs, définition, critères, mots-clés, sources |
| `build_queries` | Équations de recherche, sans les exécuter |
| `run_search` | Exécute une recherche, archive la réponse brute, la requête et la date |
| `deduplicate` | Marque les doublons (PMID, DOI, titre + année) sans rien supprimer |
| `list_records` | Liste les références par statut de tri |
| `get_record` | Référence complète, recherches d'origine, historique des décisions |
| `record_decision` | Enregistre une décision ; `human` ou `ai` (recommandation) |
| `flow_counts` | Comptes du diagramme de flux, décisions humaines uniquement |
| `export_records` | Export CSV ou RIS (Zotero, Rayyan, ASReview) |
| `import_records` | Importe un fichier .ris ou .csv d'une source manuelle |
| `check_known_items` | Contrôle du rappel sur une liste de PMID ou DOI connus |

Ressource : `protocol://eim-afrique`.

## Premier usage

1. Demander à l'assistant connecté au serveur MCP d'exécuter `run_search` sur `pubmed` puis `europepmc`, puis `deduplicate`.
2. Lancer l'application web et trier.

## Protocoles

- `protocol-v2.json` (0.2, amélioré) : actif par défaut.
- `protocol.json` (0.1) : transcription fidèle du protocole d'origine. Pour l'utiliser, définir la variable d'environnement `EIM_PROTOCOL=protocol.json`.

Chaque version écrit dans son propre dossier, pour ne jamais mélanger les résultats.

## Données

Par défaut dans `data/<version du protocole>/` : `review.db` (SQLite), `raw/` (réponses brutes des API), `exports/`. La variable d'environnement `EIM_DATA_DIR` permet de les placer ailleurs.

Le dossier `data/` n'est pas versionné : il contient les résumés des articles, soumis au droit d'auteur de leurs éditeurs, et vos décisions de tri. Chaque utilisateur reconstitue sa base en relançant les recherches.

## Limites connues

- Seules PubMed/MEDLINE et Europe PMC sont automatisées. Les autres sources (AJOL, African Index Medicus, Google Scholar, bases sous licence) passent par `import_records`.
- Un seul relecteur à la fois ; pas encore d'arbitrage entre deux relecteurs.
- Pas encore d'extraction de données.
- Les chapitres de livres indexés dans PubMed ne sont pas récupérés.

## Licence

Code sous licence MIT (voir `LICENSE`).
