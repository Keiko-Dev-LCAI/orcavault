// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  OrcaVaultV2
 * @notice Unlimited-size personal media vault on Lightchain AI.
 *         Extends V1 with chunked uploads so files of any size can be
 *         stored across multiple transactions then reassembled on playback.
 *
 * @dev    ALL user data lives in EVENT LOGS (calldata) — not in
 *         contract storage variables.  The contract only emits events;
 *         the frontend reads them back via RPC.  This keeps gas costs
 *         extremely low and makes every memory permanently readable from
 *         any standard Ethereum JSON-RPC node.
 *
 *         Two upload paths:
 *
 *         ① SMALL FILES  (base64 ≤ 90 KB)
 *            addMemory(vaultId, ..., dataURI)
 *            One transaction. V1-compatible event signature.
 *
 *         ② LARGE FILES  (base64 > 90 KB)
 *            initMemory(...)  → returns memoryId, emits MemoryCreated
 *            addChunk(memoryId, 0, chunk0)
 *            addChunk(memoryId, 1, chunk1)
 *            ...
 *            Frontend reassembles by sorting ChunkStored events by
 *            chunkIndex and concatenating chunkData strings.
 *
 * Deploy at:  https://deploy.lightchain.ai
 * Network:    Lightchain AI Mainnet  |  Chain ID: 9200
 * RPC:        https://rpc.mainnet.lightchain.ai
 */
contract OrcaVaultV2 {

    // ─────────────────────────────────────────────────────────────────
    //  STRUCTS
    // ─────────────────────────────────────────────────────────────────

    /// @notice Vault metadata (V1-compatible)
    struct VaultMeta {
        address owner;
        string  name;
        string  template;
        string  description;
        uint256 createdAt;
        bool    exists;
    }

    /// @notice Metadata for a chunked-upload memory entry
    struct MemoryMeta {
        address owner;
        string  title;
        string  description;   ///< caption / notes
        string  mediaType;     ///< "photo" | "video" | "audio" | "text" | "document"
        uint256 timestamp;
        uint256 totalChunks;
        string  template;      ///< vault template name (context only)
        bool    exists;
    }

    // ─────────────────────────────────────────────────────────────────
    //  STORAGE
    // ─────────────────────────────────────────────────────────────────

    /// @notice Vault registry (V1-compatible)
    mapping(uint256 => VaultMeta) public vaults;
    /// @dev    Next vault ID counter (starts at 1, same as V1)
    uint256 public nextVaultId = 1;
    /// @notice Number of small-file memories added to each vault
    mapping(uint256 => uint256) public vaultItemCount;

    /// @notice Chunked-memory registry
    mapping(uint256 => MemoryMeta) public memories;
    /// @dev    Next chunked-memory ID counter (starts at 1)
    uint256 public nextMemoryId = 1;

    // ─────────────────────────────────────────────────────────────────
    //  EVENTS
    // ─────────────────────────────────────────────────────────────────

    /**
     * @notice Emitted when a vault is created. V1-compatible signature.
     * @param vaultId   Unique vault identifier
     * @param owner     Wallet that created the vault
     * @param name      Display name
     * @param template  Template identifier string
     * @param description Free-text description
     * @param timestamp Block timestamp at creation
     */
    event VaultCreated(
        uint256 indexed vaultId,
        address indexed owner,
        string  name,
        string  template,
        string  description,
        uint256 timestamp
    );

    /**
     * @notice Emitted when a small file is stored in one transaction.
     *         Identical signature to V1 — frontend can query both
     *         contracts with the same filter.
     * @param vaultId   Vault the memory belongs to
     * @param itemIndex Zero-based index within the vault
     * @param addedBy   Uploader wallet address
     * @param memType   Media type string
     * @param title     Memory title
     * @param caption   Notes / caption
     * @param date      ISO date string
     * @param dataURI   Full base64 data URI of the media
     */
    event MemoryAdded(
        uint256 indexed vaultId,
        uint256 indexed itemIndex,
        address indexed addedBy,
        string  memType,
        string  title,
        string  caption,
        string  date,
        string  dataURI
    );

    /**
     * @notice Emitted when a new chunked-memory upload is initialised.
     * @param memoryId    Unique chunked-memory identifier
     * @param owner       Uploader wallet address
     * @param title       Memory title
     * @param description Caption / notes
     * @param mediaType   Media type string
     * @param totalChunks Total number of chunks that will be uploaded
     * @param template    Vault template name for display context
     * @param timestamp   Block timestamp
     */
    event MemoryCreated(
        uint256 indexed memoryId,
        address indexed owner,
        string  title,
        string  description,
        string  mediaType,
        uint256 totalChunks,
        string  template,
        uint256 timestamp
    );

    /**
     * @notice Emitted for each chunk of a large-file upload.
     *         Reassemble: query all ChunkStored events for memoryId,
     *         sort ascending by chunkIndex, concatenate chunkData.
     * @param memoryId    Chunked-memory this chunk belongs to
     * @param chunkIndex  Zero-based chunk index
     * @param totalChunks Total chunks (repeated for convenience)
     * @param chunkData   Slice of the complete base64 data URI string
     */
    event ChunkStored(
        uint256 indexed memoryId,
        uint256 indexed chunkIndex,
        uint256 totalChunks,
        string  chunkData
    );

    // ─────────────────────────────────────────────────────────────────
    //  V1-COMPATIBLE VAULT FUNCTIONS
    // ─────────────────────────────────────────────────────────────────

    /**
     * @notice Create a new vault (V1-compatible).
     * @param name        Display name for the vault
     * @param template    Template identifier (e.g. "Family Album")
     * @param description Free-text description
     * @return vaultId    The assigned vault ID (starts at 1)
     */
    function createVault(
        string memory name,
        string memory template,
        string memory description
    ) external returns (uint256) {
        uint256 id = nextVaultId++;
        vaults[id] = VaultMeta(
            msg.sender, name, template, description, block.timestamp, true
        );
        emit VaultCreated(id, msg.sender, name, template, description, block.timestamp);
        return id;
    }

    /**
     * @notice Store a small memory in a single transaction (V1-compatible).
     *         Use this path when base64 data URI length ≤ 90 KB.
     * @param vaultId  The vault to add the memory to
     * @param memType  Media type: "photo" | "video" | "audio" | "text" | "document"
     * @param title    Memory title
     * @param caption  Caption or description
     * @param date     Date string (ISO format, e.g. "2024-12-25")
     * @param dataURI  Full base64 data URI (must fit in one transaction)
     */
    function addMemory(
        uint256 vaultId,
        string memory memType,
        string memory title,
        string memory caption,
        string memory date,
        string memory dataURI
    ) external {
        require(vaults[vaultId].exists,            "Vault not found");
        require(vaults[vaultId].owner == msg.sender, "Not vault owner");
        uint256 idx = vaultItemCount[vaultId]++;
        emit MemoryAdded(vaultId, idx, msg.sender, memType, title, caption, date, dataURI);
    }

    // ─────────────────────────────────────────────────────────────────
    //  V2 CHUNKED UPLOAD FUNCTIONS
    // ─────────────────────────────────────────────────────────────────

    /**
     * @notice Initialise a chunked-memory upload.
     *         Call this ONCE before uploading chunks.  The returned
     *         memoryId must be passed to every subsequent addChunk() call.
     * @param title        Memory title
     * @param description  Caption / notes
     * @param mediaType    Media type string
     * @param totalChunks  Total number of chunks to be uploaded (≥ 1)
     * @param template     Vault template name for display context (may be empty)
     * @return memoryId    Assigned ID for this chunked memory
     */
    function initMemory(
        string memory title,
        string memory description,
        string memory mediaType,
        uint256 totalChunks,
        string memory template
    ) external returns (uint256) {
        require(totalChunks > 0, "totalChunks must be >= 1");
        uint256 id = nextMemoryId++;
        memories[id] = MemoryMeta(
            msg.sender,
            title,
            description,
            mediaType,
            block.timestamp,
            totalChunks,
            template,
            true
        );
        emit MemoryCreated(
            id,
            msg.sender,
            title,
            description,
            mediaType,
            totalChunks,
            template,
            block.timestamp
        );
        return id;
    }

    /**
     * @notice Upload one chunk of a large file.
     *         Call this once per chunk after initMemory().
     *         Chunks may be submitted in any order; the frontend sorts by
     *         chunkIndex on reassembly.
     * @param memoryId    The ID returned by initMemory()
     * @param chunkIndex  Zero-based index of this chunk
     * @param chunkData   A slice of the complete base64 data URI string
     */
    function addChunk(
        uint256 memoryId,
        uint256 chunkIndex,
        string calldata chunkData
    ) external {
        require(memories[memoryId].exists,             "Memory not found");
        require(memories[memoryId].owner == msg.sender, "Not memory owner");
        require(
            chunkIndex < memories[memoryId].totalChunks,
            "Chunk index out of range"
        );
        emit ChunkStored(
            memoryId,
            chunkIndex,
            memories[memoryId].totalChunks,
            chunkData
        );
    }

    // ─────────────────────────────────────────────────────────────────
    //  VIEW FUNCTIONS
    // ─────────────────────────────────────────────────────────────────

    /**
     * @notice Returns the total number of chunked memories ever created.
     * @return count  Equal to nextMemoryId - 1
     */
    function getMemoryCount() external view returns (uint256) {
        return nextMemoryId - 1;
    }

    /**
     * @notice Returns all chunked memory IDs created by a given address.
     *         Iterates the full registry — suitable for UI queries only.
     * @param  owner  The owner address to look up
     * @return ids    Array of chunked memory IDs belonging to owner
     */
    function getOwnerMemories(address owner)
        external
        view
        returns (uint256[] memory)
    {
        uint256 total = nextMemoryId - 1;
        // First pass: count
        uint256 count = 0;
        for (uint256 i = 1; i <= total; i++) {
            if (memories[i].owner == owner) count++;
        }
        // Second pass: collect
        uint256[] memory ids = new uint256[](count);
        uint256 j = 0;
        for (uint256 i = 1; i <= total; i++) {
            if (memories[i].owner == owner) ids[j++] = i;
        }
        return ids;
    }
}
