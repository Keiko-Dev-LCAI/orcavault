// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  OrcaVaultV3
 * @notice Adds relay-upload support so a trusted backend wallet can submit
 *         chunks on behalf of a user.  The user signs ONE authorisation
 *         message with their wallet; the backend handles all transactions.
 *
 *         New in V3:
 *         - relayWallet  address set at deploy time (Railway backend wallet)
 *         - initMemoryRelay()  — relay calls on behalf of owner
 *         - addChunkRelay()   — relay submits each chunk on behalf of owner
 *         - setRelay()        — owner of contract can update relay address
 *
 *         All V1 and V2 functions are preserved unchanged.
 *
 * Network:  Lightchain AI Mainnet  |  Chain ID: 9200
 * RPC:      https://rpc.mainnet.lightchain.ai
 * Deploy:   https://remix.ethereum.org  (MetaMask, Chrome, VPN ON)
 */
contract OrcaVaultV3 {

    // ─────────────────────────────────────────────────────────────────
    //  STRUCTS
    // ─────────────────────────────────────────────────────────────────

    struct VaultMeta {
        address owner;
        string  name;
        string  template;
        string  description;
        uint256 createdAt;
        bool    exists;
    }

    struct MemoryMeta {
        address owner;
        string  title;
        string  description;
        string  mediaType;
        uint256 timestamp;
        uint256 totalChunks;
        string  template;
        bool    exists;
    }

    // ─────────────────────────────────────────────────────────────────
    //  STORAGE
    // ─────────────────────────────────────────────────────────────────

    mapping(uint256 => VaultMeta)  public vaults;
    uint256 public nextVaultId  = 1;
    mapping(uint256 => uint256)    public vaultItemCount;

    mapping(uint256 => MemoryMeta) public memories;
    uint256 public nextMemoryId = 1;

    address public contractOwner;   // deployer wallet
    address public relayWallet;     // Railway backend wallet (set at deploy)

    // ─────────────────────────────────────────────────────────────────
    //  EVENTS  (identical signatures to V1/V2 — fully compatible)
    // ─────────────────────────────────────────────────────────────────

    event VaultCreated(
        uint256 indexed vaultId,
        address indexed owner,
        string  name,
        string  template,
        string  description,
        uint256 timestamp
    );

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

    event ChunkStored(
        uint256 indexed memoryId,
        uint256 indexed chunkIndex,
        uint256 totalChunks,
        string  chunkData
    );

    // ─────────────────────────────────────────────────────────────────
    //  MODIFIERS
    // ─────────────────────────────────────────────────────────────────

    modifier onlyRelay() {
        require(msg.sender == relayWallet, "Not relay wallet");
        _;
    }

    modifier onlyContractOwner() {
        require(msg.sender == contractOwner, "Not contract owner");
        _;
    }

    // ─────────────────────────────────────────────────────────────────
    //  CONSTRUCTOR
    // ─────────────────────────────────────────────────────────────────

    /// @param _relayWallet  Address of the Railway backend relay wallet
    constructor(address _relayWallet) {
        contractOwner = msg.sender;
        relayWallet   = _relayWallet;
    }

    // ─────────────────────────────────────────────────────────────────
    //  ADMIN
    // ─────────────────────────────────────────────────────────────────

    /// @notice Update the relay wallet address (contract owner only)
    function setRelay(address _newRelay) external onlyContractOwner {
        relayWallet = _newRelay;
    }

    // ─────────────────────────────────────────────────────────────────
    //  V1-COMPATIBLE VAULT FUNCTIONS
    // ─────────────────────────────────────────────────────────────────

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

    function addMemory(
        uint256 vaultId,
        string memory memType,
        string memory title,
        string memory caption,
        string memory date,
        string memory dataURI
    ) external {
        require(vaults[vaultId].exists,              "Vault not found");
        require(vaults[vaultId].owner == msg.sender, "Not vault owner");
        uint256 idx = vaultItemCount[vaultId]++;
        emit MemoryAdded(vaultId, idx, msg.sender, memType, title, caption, date, dataURI);
    }

    // ─────────────────────────────────────────────────────────────────
    //  V2 CHUNKED UPLOAD FUNCTIONS  (user calls directly)
    // ─────────────────────────────────────────────────────────────────

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
            msg.sender, title, description, mediaType,
            block.timestamp, totalChunks, template, true
        );
        emit MemoryCreated(
            id, msg.sender, title, description,
            mediaType, totalChunks, template, block.timestamp
        );
        return id;
    }

    function addChunk(
        uint256 memoryId,
        uint256 chunkIndex,
        string calldata chunkData
    ) external {
        require(memories[memoryId].exists,              "Memory not found");
        require(memories[memoryId].owner == msg.sender, "Not memory owner");
        require(chunkIndex < memories[memoryId].totalChunks, "Chunk index out of range");
        emit ChunkStored(
            memoryId, chunkIndex,
            memories[memoryId].totalChunks,
            chunkData
        );
    }

    // ─────────────────────────────────────────────────────────────────
    //  V3 RELAY FUNCTIONS  (Railway backend calls on user's behalf)
    // ─────────────────────────────────────────────────────────────────

    /**
     * @notice Relay: initialise a chunked-memory upload on behalf of owner.
     *         Only callable by the whitelisted relay wallet.
     * @param  owner       The user's wallet address (recorded as memory owner)
     * @param  title       Memory title
     * @param  description Caption / notes
     * @param  mediaType   Media type string
     * @param  totalChunks Total number of chunks to be uploaded
     * @param  template    Vault template name
     * @return memoryId    Assigned ID for this chunked memory
     */
    function initMemoryRelay(
        address owner,
        string memory title,
        string memory description,
        string memory mediaType,
        uint256 totalChunks,
        string memory template
    ) external onlyRelay returns (uint256) {
        require(totalChunks > 0, "totalChunks must be >= 1");
        uint256 id = nextMemoryId++;
        memories[id] = MemoryMeta(
            owner, title, description, mediaType,
            block.timestamp, totalChunks, template, true
        );
        emit MemoryCreated(
            id, owner, title, description,
            mediaType, totalChunks, template, block.timestamp
        );
        return id;
    }

    /**
     * @notice Relay: submit one chunk on behalf of the memory owner.
     *         Only callable by the whitelisted relay wallet.
     * @param  memoryId   The ID returned by initMemoryRelay()
     * @param  chunkIndex Zero-based chunk index
     * @param  chunkData  Slice of the complete base64 data URI string
     */
    function addChunkRelay(
        uint256 memoryId,
        uint256 chunkIndex,
        string calldata chunkData
    ) external onlyRelay {
        require(memories[memoryId].exists, "Memory not found");
        require(chunkIndex < memories[memoryId].totalChunks, "Chunk index out of range");
        emit ChunkStored(
            memoryId, chunkIndex,
            memories[memoryId].totalChunks,
            chunkData
        );
    }

    // ─────────────────────────────────────────────────────────────────
    //  VIEW FUNCTIONS
    // ─────────────────────────────────────────────────────────────────

    function getMemoryCount() external view returns (uint256) {
        return nextMemoryId - 1;
    }

    function getOwnerMemories(address owner)
        external view returns (uint256[] memory)
    {
        uint256 total = nextMemoryId - 1;
        uint256 count = 0;
        for (uint256 i = 1; i <= total; i++) {
            if (memories[i].owner == owner) count++;
        }
        uint256[] memory ids = new uint256[](count);
        uint256 j = 0;
        for (uint256 i = 1; i <= total; i++) {
            if (memories[i].owner == owner) ids[j++] = i;
        }
        return ids;
    }
}
