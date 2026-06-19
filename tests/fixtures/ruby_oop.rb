require 'json'
require_relative '../lib/foo'

CONFIG = Settings.load("x")

module Auth
  MAX_RETRIES = 3

  class User
    include Comparable
    extend ClassMethods
    prepend Logging

    attr_accessor :name
    attr_reader :id
    attr_writer :token

    def initialize(id)
      @id = id
    end

    def name=(value)
      @name = value
    end

    def self.build(id)
      new(id)
    end

    class << self
      def from_token(token)
        new(token)
      end
    end

    private

    def secret
      @token
    end
  end

  class Admin < User
    def elevate
      save
    end
  end
end

class Admin::Audit < Auth::Admin
end
