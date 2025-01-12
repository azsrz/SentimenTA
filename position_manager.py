import os
from datetime import datetime
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

class Position:
    def __init__(self, symbol, qty, entry_price, side, entry_time):
        self.symbol = symbol
        self.qty = float(qty)
        self.entry_price = float(entry_price)
        self.side = side
        self.entry_time = entry_time
        self.target_qty = float(qty)  # For gradual position building/reduction
        self.pl_pct = 0  # Current P&L percentage
        self.current_price = entry_price
        
    def update_pl(self, current_price):
        """Update position P&L"""
        self.current_price = float(current_price)
        multiplier = 1 if self.side == OrderSide.BUY else -1
        self.pl_pct = ((self.current_price / self.entry_price) - 1) * multiplier
        
    def get_exposure(self, equity):
        """Calculate position exposure as percentage of equity"""
        position_value = abs(self.qty * self.current_price)
        return position_value / equity
        
    def __str__(self):
        return (f"{self.symbol}: {self.qty} shares @ ${self.entry_price:.2f} "
                f"({self.pl_pct:.1%} P&L)")

class PositionManager:
    def __init__(self):
        load_dotenv()
        api_key = os.getenv('ALPACA_API_KEY')
        api_secret = os.getenv('ALPACA_SECRET_KEY')
        self.trading_client = TradingClient(api_key, api_secret, paper=True)
        self.positions = {}  # symbol -> Position object
        self.pending_closes = set()  # Symbols with pending close orders
        self.pending_orders = []  # List of pending new position orders
        
        # Position sizing parameters
        self.max_position_size = 0.08  # 8% max per position
        self.position_step_size = 0.02  # 2% per trade for gradual building
        self.max_total_exposure = 1.6  # 160% total exposure (80% long + 80% short)
        
        # Initialize current positions and clean up any old pending orders
        self.trading_client.cancel_orders()  # Cancel any old pending orders
        self.update_positions()
    
    def get_account_info(self):
        """Get account information"""
        account = self.trading_client.get_account()
        return {
            'equity': float(account.equity),
            'buying_power': float(account.buying_power),
            'initial_margin': float(account.initial_margin),
            'margin_multiplier': float(account.multiplier),
            'daytrading_buying_power': float(account.daytrading_buying_power)
        }
    
    def update_positions(self):
        """Update position tracking with current market data"""
        try:
            alpaca_positions = self.trading_client.get_all_positions()
            current_symbols = set()
            
            # Update existing positions and add new ones
            for p in alpaca_positions:
                symbol = p.symbol
                current_symbols.add(symbol)
                qty = float(p.qty)
                current_price = float(p.current_price)
                entry_price = float(p.avg_entry_price)
                side = OrderSide.BUY if qty > 0 else OrderSide.SELL
                
                if symbol not in self.positions:
                    # New position
                    self.positions[symbol] = Position(
                        symbol, qty, entry_price, side, 
                        datetime.now()  # Approximate entry time for existing positions
                    )
                
                # Update position data
                pos = self.positions[symbol]
                pos.qty = qty
                pos.entry_price = entry_price
                pos.update_pl(current_price)
            
            # Remove closed positions
            self.positions = {s: p for s, p in self.positions.items() if s in current_symbols}
            
            # Calculate total exposure excluding pending closes
            account = self.get_account_info()
            active_positions = {s: p for s, p in self.positions.items() 
                              if s not in self.pending_closes}
            total_exposure = sum(p.get_exposure(account['equity']) 
                               for p in active_positions.values())
            
            print("\nCurrent Portfolio Status:")
            print(f"Total Exposure: {total_exposure:.1%}")
            for pos in active_positions.values():
                exposure = pos.get_exposure(account['equity'])
                print(f"{pos} ({exposure:.1%} exposure)")
            
            if self.pending_closes:
                print("\nPending Close Orders:")
                for symbol in self.pending_closes:
                    print(f"- {symbol}")
            
            if self.pending_orders:
                print("\nPending New Orders:")
                for order in self.pending_orders:
                    print(f"- {order['symbol']} ({order['side']})")
                
            return self.positions
            
        except Exception as e:
            print(f"Error updating positions: {str(e)}")
            return {}
    
    def calculate_target_position(self, symbol, price, side):
        """
        Calculate target position size considering existing positions
        Returns target shares and whether to allow the trade
        """
        account = self.get_account_info()
        equity = account['equity']
        
        # Calculate current total exposure excluding pending closes
        active_positions = {s: p for s, p in self.positions.items() 
                          if s not in self.pending_closes}
        total_exposure = sum(p.get_exposure(equity) for p in active_positions.values())
        
        # Check if we're already at max exposure
        if total_exposure >= self.max_total_exposure:
            print(f"Maximum total exposure reached: {total_exposure:.1%}")
            return 0, False
        
        # Calculate available position size
        target_position_value = equity * self.max_position_size
        current_position = active_positions.get(symbol)
        
        if current_position:
            # Position exists - check if we should add more
            current_exposure = current_position.get_exposure(equity)
            
            # Don't add if already at max size
            if current_exposure >= self.max_position_size:
                print(f"Maximum position size reached for {symbol}: {current_exposure:.1%}")
                return 0, False
            
            # Don't add if position moving against us
            if current_position.pl_pct < -0.02:  # -2% loss threshold
                print(f"Position moving against us: {current_position.pl_pct:.1%} P&L")
                return 0, False
            
            # Calculate room for addition
            remaining_size = target_position_value - (current_position.qty * price)
            step_size = equity * self.position_step_size
            target_addition = min(remaining_size, step_size)
            
            return int(target_addition / price), True
            
        else:
            # New position - start with one step
            step_size = equity * self.position_step_size
            return int(step_size / price), True
    
    def should_close_position(self, symbol, technical_data):
        """Determine if a position should be closed based on technical analysis"""
        position = self.positions.get(symbol)
        if not position:
            return False
            
        # Get current exposure
        account = self.get_account_info()
        total_exposure = sum(p.get_exposure(float(account['equity'])) 
                           for p in self.positions.values())
        
        # Close if any of these conditions are met:
        reasons = []
        
        # 1. Significant loss
        if position.pl_pct < -0.05:  # -5% stop loss
            reasons.append(f"Stop loss hit: {position.pl_pct:.1%} P&L")
        
        # 2. Technical score moves against position
        technical_score = technical_data['score']
        if position.side == OrderSide.BUY and technical_score < 0.4:
            reasons.append(f"Weak technical score for long: {technical_score:.2f}")
        elif position.side == OrderSide.SELL and technical_score > 0.6:
            reasons.append(f"Strong technical score for short: {technical_score:.2f}")
        
        # 3. Momentum moves against position
        momentum = technical_data['momentum']
        if position.side == OrderSide.BUY and momentum < -0.02:  # -2% momentum for longs
            reasons.append(f"Negative momentum for long: {momentum:.1f}%")
        elif position.side == OrderSide.SELL and momentum > 0.02:  # +2% momentum for shorts
            reasons.append(f"Positive momentum for short: {momentum:.1f}%")
        
        # 4. Over exposure - close weakest positions
        if total_exposure > self.max_total_exposure:
            # Close positions with weak technicals when over-exposed
            if (position.side == OrderSide.BUY and technical_score < 0.5) or \
               (position.side == OrderSide.SELL and technical_score > 0.5):
                reasons.append(f"Reducing exposure ({total_exposure:.1%} total)")
        
        # 5. Mediocre performance with significant age
        position_age = (datetime.now() - position.entry_time).days
        if position_age > 5 and abs(position.pl_pct) < 0.01:
            reasons.append(f"Stagnant position after {position_age} days")
        
        if reasons:
            reason_str = ", ".join(reasons)
            print(f"Closing {symbol} due to: {reason_str}")
            return True
            
        return False
    
    def place_order(self, symbol, shares, side=OrderSide.BUY):
        """Place a market order"""
        if shares <= 0:
            return None
            
        order_details = MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        
        try:
            print(f"\nSubmitting order to Alpaca:")
            print(f"Symbol: {symbol}")
            print(f"Shares: {shares}")
            print(f"Side: {side}")
            print(f"Time in force: {TimeInForce.DAY}")
            
            order = self.trading_client.submit_order(order_details)
            
            print("\nOrder response from Alpaca:")
            print(f"Order ID: {order.id}")
            print(f"Status: {order.status}")
            print(f"Created at: {order.created_at}")
            
            # Verify order was created
            try:
                order_status = self.trading_client.get_order_by_id(order.id)
                print(f"Order verification - Status: {order_status.status}")
                
                if order_status.status == 'accepted':
                    # Track pending order
                    self.pending_orders.append({
                        'symbol': symbol,
                        'shares': shares,
                        'side': side,
                        'order_id': order.id
                    })
                    print("Order accepted - will execute when market opens")
            except Exception as e:
                print(f"Error verifying order: {str(e)}")
            
            return order
        except Exception as e:
            print(f"\nError placing order:")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
            return None
    
    def close_position(self, symbol):
        """Close an existing position"""
        try:
            order = self.trading_client.close_position(symbol)
            print(f"\nClosing position in {symbol}:")
            print(f"Order ID: {order.id}")
            print(f"Status: {order.status}")
            print(f"Created at: {order.created_at}")
            
            # Verify order was created
            try:
                order_status = self.trading_client.get_order_by_id(order.id)
                print(f"Order verification - Status: {order_status.status}")
                
                if order_status.status == 'accepted':
                    print("Close order accepted - will execute when market opens")
                    self.pending_closes.add(symbol)
                else:
                    print(f"Warning: Close order status is {order_status.status}")
            except Exception as e:
                print(f"Error verifying order: {str(e)}")
                
        except Exception as e:
            print(f"\nError closing position in {symbol}:")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
