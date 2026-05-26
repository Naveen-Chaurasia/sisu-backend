import os

NEO4J_URI  = os.getenv("NEO4J_URI",  "bolt://localhost:7687")
NEO4J_AUTH = (
    os.getenv("NEO4J_USER",     "neo4j"),
    os.getenv("NEO4J_PASSWORD", "neo4jsis"),
)
CSV_PATH   = os.getenv(
    "CSV_PATH",
    r"d:\pythone\RAG Project\sisepuede\sisepuede\sisepuede\ref\examples\input_data_frame.csv",
)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))

# App user credentials — override via APP_USERS_<NAME>=<password> env vars
USERS: dict[str, str] = {
    "naveen": os.getenv("APP_PASSWORD_NAVEEN", "Naveen"),
    "baz":    os.getenv("APP_PASSWORD_BAZ",    "Su3y7kads#93"),
}
