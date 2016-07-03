import rlp
from ethereum.utils import normalize_address, hash32, trie_root, \
    big_endian_int, address, int256, encode_int, \
    safe_ord, int_to_addr
from rlp.sedes import big_endian_int, Binary, binary, CountableList
from rlp.utils import decode_hex, encode_hex, ascii_chr
from ethereum import utils
from ethereum import trie
from ethereum import bloom
from ethereum import transactions
from ethereum.trie import Trie
from ethereum.securetrie import SecureTrie
from ethereum import opcodes
from ethereum.state import get_block
from ethereum.processblock import apply_msg, create_contract, _apply_msg, Log
from ethereum import vm
from config import default_config
from db import BaseDB, EphemDB
from ethereum.exceptions import InvalidNonce, InsufficientStartGas, UnsignedTransaction, \
        BlockGasLimitReached, InsufficientBalance
import sys
if sys.version_info.major == 2:
    from repoze.lru import lru_cache
else:
    from functools import lru_cache
from ethereum.slogging import get_logger

log = get_logger('eth.block')
log_tx = get_logger('eth.pb.tx')
log_msg = get_logger('eth.pb.msg')
log_state = get_logger('eth.pb.msg.state')

# contract creating transactions send to an empty address
CREATE_CONTRACT_ADDRESS = b''

VERIFIERS = {
    'ethash': lambda state, header: header.check_pow(),
    'contract': lambda state, header: not not apply_const_message(state, vm.Message('\xff' * 20, int_to_addr(255), 0, 1000000, header.signing_hash()+header.extra_data, code_address=int_to_addr(255)))
}

def initialize(state, block):
    pre_root = state.trie.root_hash or ('\x00' * 32)
    state.txindex = 0
    state.gas_used = 0
    state.bloom = 0
    state.timestamp = block.header.timestamp
    state.gas_limit = block.header.gas_limit
    state.block_number = block.header.number
    state.recent_uncles[state.block_number] = [x.hash for x in block.uncles]
    state.block_coinbase = block.header.coinbase
    state.block_difficulty = block.header.difficulty
    if state.block_number == state.config["METROPOLIS_FORK_BLKNUM"]:
        self.set_code(utils.normalize_address(state.config["METROPOLIS_STATEROOT_STORE"]), state.config["METROPOLIS_GETTER_CODE"])
        self.set_code(utils.normalize_address(state.config["METROPOLIS_BLOCKHASH_STORE"]), state.config["METROPOLIS_GETTER_CODE"])
    if state.block_number >= state.config["METROPOLIS_FORK_BLKNUM"]:
        self.set_storage_data(utils.normalize_address(state.config["METROPOLIS_STATEROOT_STORE"]),
                              state.block_number % state.config["METROPOLIS_WRAPAROUND"],
                              pre_root)
        self.set_storage_data(utils.normalize_address(state.config["METROPOLIS_BLOCKHASH_STORE"]),
                              state.block_number % state.config["METROPOLIS_WRAPAROUND"],
                              state.prev_headers[0].hash if state.prev_headers else '\x00' * 32)

def finalize(state, block):
    """Apply rewards and commit."""
    delta = int(state.config['BLOCK_REWARD'] + state.config['NEPHEW_REWARD'] * len(block.uncles))
    state.delta_balance(state.block_coinbase, delta)

    br = state.config['BLOCK_REWARD']
    udpf = state.config['UNCLE_DEPTH_PENALTY_FACTOR']

    for uncle in block.uncles:
        r = int(br * (udpf + uncle.number - state.block_number) // udpf)

        state.delta_balance(uncle.coinbase, r)
    if state.block_number - state.config['MAX_UNCLE_DEPTH'] in state.recent_uncles:
        del state.recent_uncles[state.block_number - state.config['MAX_UNCLE_DEPTH']]
    state.commit()
    state.add_block_header(block.header)


def apply_block(state, block, creating=False):
    # Pre-processing and verification
    initialize(state, block)
    assert validate_block_header(state, block.header)
    assert validate_uncles(state, block)
    receipts = []
    # Process transactions
    for tx in block.transactions:
        success, output, logs = apply_transaction(state, tx)
        if state.block_number >= state.config["METROPOLIS_FORK_BLKNUM"]:
            r = Receipt('\x00' * 32, state.gas_used, logs)
        else:
            r = Receipt(state.trie.root_hash, state.gas_used, logs)
        receipts.append(r)
        state.bloom |= r.bloom  # int
        state.txindex += 1
    # Finalize (incl paying block rewards)
    finalize(state, block)
    # Verify state root, tx list root, receipt root
    if creating:
        block.header.receipts_root = mk_receipt_sha(receipts)
        block.header.tx_list_root = mk_transaction_sha(block.transactions)
        block.header.state_root = state.trie.root_hash
    else:
        assert block.header.receipts_root == mk_receipt_sha(receipts), (block.header.receipts_root, mk_receipt_sha(receipts), receipts)
        assert block.header.tx_list_root == mk_transaction_sha(block.transactions)
        assert block.header.state_root == state.trie.root_hash
    return state, receipts

def validate_transaction(state, tx):

    def rp(what, actual, target):
        return '%r: %r actual:%r target:%r' % (tx, what, actual, target)

    # (1) The transaction signature is valid;
    if not tx.sender:  # sender is set and validated on Transaction initialization
        if state.block_number >= state.config["METROPOLIS_FORK_BLKNUM"]:
            tx._sender = normalize_address(state.config["METROPOLIS_ENTRY_POINT"])
        else:
            raise UnsignedTransaction(tx)
    if state.block_number >= state.config["HOMESTEAD_FORK_BLKNUM"]:
            tx.check_low_s()

    # (2) the transaction nonce is valid (equivalent to the
    #     sender account's current nonce);
    acctnonce = state.get_nonce(tx.sender)
    if acctnonce != tx.nonce:
        raise InvalidNonce(rp('nonce', tx.nonce, acctnonce))

    # (3) the gas limit is no smaller than the intrinsic gas,
    # g0, used by the transaction;
    if tx.startgas < tx.intrinsic_gas_used:
        raise InsufficientStartGas(rp('startgas', tx.startgas, tx.intrinsic_gas_used))

    # (4) the sender account balance contains at least the
    # cost, v0, required in up-front payment.
    total_cost = tx.value + tx.gasprice * tx.startgas
    if state.get_balance(tx.sender) < total_cost:
        raise InsufficientBalance(rp('balance', state.get_balance(tx.sender), total_cost))

    # check block gas limit
    if state.gas_used + tx.startgas > state.gas_limit:
        raise BlockGasLimitReached(rp('gaslimit', state.gas_used + tx.startgas, state.gas_limit))

    return True


def apply_const_message(state, msg):
    state1 = state.ephemeral_clone()
    ext = VMExt(state1, tx)
    result, gas_remained, data = apply_msg(ext, message)
    return data if result else None


def apply_transaction(state, tx):
    state.logs = []
    state.suicides = []
    state.refunds = 0
    validate_transaction(state, tx)

    # print(block.get_nonce(tx.sender), '@@@')

    def rp(what, actual, target):
        return '%r: %r actual:%r target:%r' % (tx, what, actual, target)

    intrinsic_gas = tx.intrinsic_gas_used
    if state.block_number >= state.config['HOMESTEAD_FORK_BLKNUM']:
        assert tx.s * 2 < transactions.secpk1n
        if not tx.to or tx.to == CREATE_CONTRACT_ADDRESS:
            intrinsic_gas += opcodes.CREATE[3]
            if tx.startgas < intrinsic_gas:
                raise InsufficientStartGas(rp('startgas', tx.startgas, intrinsic_gas))

    log_tx.debug('TX NEW', tx_dict=tx.log_dict())
    # start transacting #################
    state.increment_nonce(tx.sender)

    # buy startgas
    assert state.get_balance(tx.sender) >= tx.startgas * tx.gasprice
    state.delta_balance(tx.sender, -tx.startgas * tx.gasprice)
    message_gas = tx.startgas - intrinsic_gas
    message_data = vm.CallData([safe_ord(x) for x in tx.data], 0, len(tx.data))
    message = vm.Message(tx.sender, tx.to, tx.value, message_gas, message_data, code_address=tx.to)

    # MESSAGE
    ext = VMExt(state, tx)
    if tx.to and tx.to != CREATE_CONTRACT_ADDRESS:
        result, gas_remained, data = apply_msg(ext, message)
        log_tx.debug('_res_', result=result, gas_remained=gas_remained, data=data)
    else:  # CREATE
        result, gas_remained, data = create_contract(ext, message)
        assert utils.is_numeric(gas_remained)
        log_tx.debug('_create_', result=result, gas_remained=gas_remained, data=data)

    assert gas_remained >= 0

    log_tx.debug("TX APPLIED", result=result, gas_remained=gas_remained,
                 data=data)

    if not result:  # 0 = OOG failure in both cases
        log_tx.debug('TX FAILED', reason='out of gas',
                     startgas=tx.startgas, gas_remained=gas_remained)
        state.gas_used += tx.startgas
        state.delta_balance(state.block_coinbase, tx.gasprice * tx.startgas)
        output = b''
        success = 0
    else:
        log_tx.debug('TX SUCCESS', data=data)
        gas_used = tx.startgas - gas_remained
        state.refunds += len(set(state.suicides)) * opcodes.GSUICIDEREFUND
        if state.refunds > 0:
            log_tx.debug('Refunding', gas_refunded=min(state.refunds, gas_used // 2))
            gas_remained += min(state.refunds, gas_used // 2)
            gas_used -= min(state.refunds, gas_used // 2)
            state.refunds = 0
        # sell remaining gas
        state.delta_balance(tx.sender, tx.gasprice * gas_remained)
        state.delta_balance(state.block_coinbase, tx.gasprice * gas_used)
        state.gas_used += gas_used
        if tx.to:
            output = b''.join(map(ascii_chr, data))
        else:
            output = data
        success = 1
    suicides = state.suicides
    state.suicides = []
    for s in suicides:
        state.set_balance(s, 0)
        state.del_account(s)
    logs = state.logs
    state.logs = []
    if state.block_number < state.config['METROPOLIS_FORK_BLKNUM']:
        state.commit()
    return success, output, logs

def mk_receipt_sha(receipts):
    t = trie.Trie(EphemDB())
    for i, receipt in enumerate(receipts):
        t.update(rlp.encode(i), rlp.encode(receipt))
        # print i, rlp.decode(rlp.encode(receipt))
    return t.root_hash

mk_transaction_sha = mk_receipt_sha

def validate_block_header(state, header):
    assert VERIFIERS[state.config['CONSENSUS_ALGO']](state, header)
    parent = state.prev_headers[0]
    if parent:
        if header.prevhash != parent.hash:
            raise ValueError("Block's prevhash and parent's hash do not match")
        if header.number != parent.number + 1:
            raise ValueError("Block's number is not the successor of its parent number")
        if not check_gaslimit(parent, header.gas_limit, config=state.config):
            raise ValueError("Block's gaslimit is inconsistent with its parent's gaslimit")
        if header.difficulty != calc_difficulty(parent, header.timestamp, config=state.config):
            raise ValueError("Block's difficulty is inconsistent with its parent's difficulty")
        if header.gas_used > header.gas_limit:
            raise ValueError("Gas used exceeds gas limit")
        if header.timestamp <= parent.timestamp:
            raise ValueError("Timestamp equal to or before parent")
        if header.timestamp >= 2**256:
            raise ValueError("Timestamp waaaaaaaaaaayy too large")
    return True

def validate_block(state, block):
    state_prime, receipts = apply_block(state, block)


# Gas limit adjustment algo
def calc_gaslimit(parent, config=default_config):
    decay = parent.gas_limit // config['GASLIMIT_EMA_FACTOR']
    new_contribution = ((parent.gas_used * config['BLKLIM_FACTOR_NOM']) //
                        config['BLKLIM_FACTOR_DEN'] // config['GASLIMIT_EMA_FACTOR'])
    gl = max(parent.gas_limit - decay + new_contribution, config['MIN_GAS_LIMIT'])
    if gl < config['GENESIS_GAS_LIMIT']:
        gl2 = parent.gas_limit + decay
        gl = min(config['GENESIS_GAS_LIMIT'], gl2)
    assert check_gaslimit(parent, gl, config=config)
    return gl


def check_gaslimit(parent, gas_limit, config=default_config):
    #  block.gasLimit - parent.gasLimit <= parent.gasLimit // GasLimitBoundDivisor
    gl = parent.gas_limit // config['GASLIMIT_ADJMAX_FACTOR']
    a = bool(abs(gas_limit - parent.gas_limit) <= gl)
    b = bool(gas_limit >= config['MIN_GAS_LIMIT'])
    return a and b


# Difficulty adjustment algo
def calc_difficulty(parent, timestamp, config=default_config):
    offset = parent.difficulty // config['BLOCK_DIFF_FACTOR']
    if parent.number >= (config['METROPOLIS_FORK_BLKNUM'] - 1):
        sign = max(len(parent.uncles) - ((timestamp - parent.timestamp) // config['METROPOLIS_DIFF_ADJUSTMENT_CUTOFF']), -99)
    elif parent.number >= (config['HOMESTEAD_FORK_BLKNUM'] - 1):
        sign = max(1 - ((timestamp - parent.timestamp) // config['HOMESTEAD_DIFF_ADJUSTMENT_CUTOFF']), -99)
    else:
        sign = 1 if timestamp - parent.timestamp < config['DIFF_ADJUSTMENT_CUTOFF'] else -1
    # If we enter a special mode where the genesis difficulty starts off below
    # the minimal difficulty, we allow low-difficulty blocks (this will never
    # happen in the official protocol)
    o = int(max(parent.difficulty + offset * sign, min(parent.difficulty, config['MIN_DIFF'])))
    period_count = (parent.number + 1) // config['EXPDIFF_PERIOD']
    if period_count >= config['EXPDIFF_FREE_PERIODS']:
        o = max(o + 2**(period_count - config['EXPDIFF_FREE_PERIODS']), config['MIN_DIFF'])
    return o


def validate_uncles(state, block):
    """Validate the uncles of this block."""
    # Make sure hash matches up
    if utils.sha3(rlp.encode(block.uncles)) != block.header.uncles_hash:
        return False
    # Enforce maximum number of uncles
    if len(block.uncles) > state.config['MAX_UNCLES']:
        return False
    # Uncle must have lower block number than blockj
    for uncle in block.uncles:
        assert uncle.number < block.header.number

    # Check uncle validity
    MAX_UNCLE_DEPTH = state.config['MAX_UNCLE_DEPTH']
    ancestor_chain = [block.header] + [a for a in state.prev_headers[:MAX_UNCLE_DEPTH + 1] if a]
    assert len(ancestor_chain) == min(block.header.number + 1, MAX_UNCLE_DEPTH + 2)
    # Uncles of this block cannot be direct ancestors and cannot also
    # be uncles included 1-6 blocks ago
    ineligible = [b.hash for b in ancestor_chain]
    for blknum, uncles in state.recent_uncles.items():
        if state.block_number > blknum >= state.block_number - MAX_UNCLE_DEPTH:
            ineligible.extend([u for u in uncles])
    eligible_ancestor_hashes = [x.hash for x in ancestor_chain[2:]]
    for uncle in block.uncles:
        if uncle.prevhash not in eligible_ancestor_hashes:
            log.error("Uncle does not have a valid ancestor", block=self,
                      eligible=[encode_hex(x) for x in eligible_ancestor_hashes],
                      uncle_prevhash=encode_hex(uncle.prevhash))
            return False
        parent = [x for x in ancestor_chain if x.hash == uncle.prevhash][0]
        if uncle.difficulty != calc_difficulty(parent, uncle.timestamp, config=state.config):
            return False
        if uncle.number != parent.number + 1:
            return False
        if uncle.timestamp < parent.timestamp:
            return False
        if not uncle.check_pow():
            return False
        if uncle.hash in ineligible:
            log.error("Duplicate uncle", block=self,
                      uncle=encode_hex(utils.sha3(rlp.encode(uncle))))
            return False
        ineligible.append(uncle.hash)
    return True


class VMExt():

    def __init__(self, state, tx):
        self._state = state
        self.get_code = state.get_code
        self.set_code = state.set_code
        self.get_balance = state.get_balance
        self.set_balance = state.set_balance
        self.get_nonce = state.get_nonce
        self.set_nonce = state.set_nonce
        self.increment_nonce = state.increment_nonce
        self.set_storage_data = state.set_storage_data
        self.get_storage_data = state.get_storage_data
        self.get_storage_bytes = state.get_storage_bytes
        self.log_storage = lambda x: 'storage logging stub'
        self.add_suicide = lambda x: state.add_suicide(x)
        self.add_refund = lambda x: \
            state.set_param('refunds', state.refunds + x)
        self.block_hash = lambda x: state.get_block_hash(state.block_number - x - 1) \
            if (1 <= state.block_number - x <= 256 and x <= state.block_number) else b''
        self.block_coinbase = state.block_coinbase
        self.block_timestamp = state.timestamp
        self.block_number = state.block_number
        self.block_difficulty = state.block_difficulty
        self.block_gas_limit = state.gas_limit
        self.log = lambda addr, topics, data: \
            state.add_log(Log(addr, topics, data))
        self.create = lambda msg: create_contract(self, msg)
        self.msg = lambda msg: _apply_msg(self, msg, self.get_code(msg.code_address))
        self.account_exists = state.account_exists
        self.post_homestead_hardfork = lambda: state.block_number >= state.config['HOMESTEAD_FORK_BLKNUM']
        self.post_metropolis_hardfork = lambda: state.block_number >= state.config['METROPOLIS_FORK_BLKNUM']
        self.snapshot = state.snapshot
        self.revert = state.revert
        self.transfer_value = state.transfer_value
        self.reset_storage = state.reset_storage
        self.tx_origin = tx.sender if tx else '\x00'*32
        self.tx_gasprice = tx.gasprice if tx else 0


class Receipt(rlp.Serializable):

    fields = [
        ('state_root', trie_root),
        ('gas_used', big_endian_int),
        ('bloom', int256),
        ('logs', CountableList(Log))
    ]

    def __init__(self, state_root, gas_used, logs, bloom=None):
        # does not call super.__init__ as bloom should not be an attribute but a property
        self.state_root = state_root
        self.gas_used = gas_used
        self.logs = logs
        if bloom is not None and bloom != self.bloom:
            raise ValueError("Invalid bloom filter")
        self._cached_rlp = None
        self._mutable = True

    @property
    def bloom(self):
        bloomables = [x.bloomables() for x in self.logs]
        return bloom.bloom_from_list(utils.flatten(bloomables))
