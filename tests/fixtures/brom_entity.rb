class Account < ApplicationModel
  attribute :id
  attribute :amount_cents, :decimal
  attribute :created_at, :datetime, default: nil

  def increment(amount)
    self.amount_cents += amount
  end
end
